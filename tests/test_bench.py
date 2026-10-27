import math
import struct

import pytest

from pokescan.bench import format_report, full_res_shots, jpeg_size, percentile, run_phase
from pokescan.obs import ObsError


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


def fake_jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + bytes(9)
    sof0 = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 3) + bytes(3)
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


class FakeObs:
    """Renders at 60 fps on the fake clock; each screenshot takes `latency` s
    and makes OBS skip `skips_per_shot` render frames."""

    def __init__(self, clock: FakeClock, latency: float = 0.05, skips_per_shot: int = 0, fail_every: int = 0):
        self.clock = clock
        self.latency = latency
        self.skips_per_shot = skips_per_shot
        self.fail_every = fail_every
        self.shots = []
        self.skipped = 0

    async def screenshot(self, source, width=None, fmt="jpg", quality=85):
        self.shots.append((self.clock(), width))
        self.clock.t += self.latency
        self.skipped += self.skips_per_shot
        if self.fail_every and len(self.shots) % self.fail_every == 0:
            raise ObsError("GetSourceScreenshot failed (600): boom")
        return fake_jpeg(width or 1920, (width or 1920) * 9 // 16)

    async def stats(self):
        frames = int(self.clock() * 60)
        return {
            "averageFrameRenderTime": 1.5,
            "renderSkippedFrames": self.skipped,
            "renderTotalFrames": frames,
            "outputSkippedFrames": 0,
            "outputTotalFrames": 0,
        }


def test_percentile_nearest_rank():
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 5.0
    assert percentile(values, 0) == 1.0
    assert math.isnan(percentile([], 50))


def test_jpeg_size_reads_sof_marker():
    assert jpeg_size(fake_jpeg(640, 360)) == (640, 360)
    assert jpeg_size(b"\x89PNG\r\n") is None


async def test_idle_phase_takes_no_screenshots():
    clock = FakeClock()
    obs = FakeObs(clock)
    result = await run_phase(obs, "idle", 3.0, clock=clock, sleep=clock.sleep)
    assert obs.shots == []
    assert result.render_total == pytest.approx(180, abs=1)
    assert len(result.render_ms_samples) >= 3


async def test_polls_at_target_rate_when_obs_keeps_up():
    clock = FakeClock()
    obs = FakeObs(clock, latency=0.05)
    result = await run_phase(obs, "5 Hz", 4.0, "Card Cam", hz=5, width=640, clock=clock, sleep=clock.sleep)
    assert len(result.latencies_ms) == 20
    assert result.achieved_hz == pytest.approx(5, abs=0.3)
    assert result.latencies_ms[0] == pytest.approx(50)
    assert result.frame_size == (640, 360)
    assert {width for _, width in obs.shots} == {640}


async def test_slow_screenshots_run_back_to_back_without_bursting():
    clock = FakeClock()
    obs = FakeObs(clock, latency=0.3)
    result = await run_phase(obs, "10 Hz", 3.0, "Card Cam", hz=10, width=640, clock=clock, sleep=clock.sleep)
    starts = [t for t, _ in obs.shots]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    # One in flight: never closer together than one screenshot's latency.
    assert min(gaps) >= 0.3 - 1e-9
    assert result.achieved_hz == pytest.approx(1 / 0.3, abs=0.4)


async def test_counts_skipped_render_frames_caused_by_polling():
    clock = FakeClock()
    obs = FakeObs(clock, latency=0.05, skips_per_shot=1)
    result = await run_phase(obs, "2 Hz", 5.0, "Card Cam", hz=2, width=640, clock=clock, sleep=clock.sleep)
    assert result.render_skipped == len(obs.shots) == 10
    assert result.render_skipped_pct == pytest.approx(100 * 10 / 300, abs=0.5)


async def test_screenshot_errors_are_counted_not_raised():
    clock = FakeClock()
    obs = FakeObs(clock, fail_every=2)
    result = await run_phase(obs, "5 Hz", 2.0, "Card Cam", hz=5, width=640, clock=clock, sleep=clock.sleep)
    assert len(result.errors) == 5
    assert len(result.latencies_ms) == 5


async def test_full_res_shots_use_native_width():
    clock = FakeClock()
    obs = FakeObs(clock, latency=0.12)
    result = await full_res_shots(obs, "Card Cam", 4, clock=clock)
    assert [width for _, width in obs.shots] == [None] * 4
    assert result.frame_size == (1920, 1080)
    assert result.latencies_ms == pytest.approx([120] * 4)


async def test_report_lists_every_phase_and_error():
    clock = FakeClock()
    obs = FakeObs(clock, fail_every=3)
    idle = await run_phase(obs, "idle", 1.0, clock=clock, sleep=clock.sleep)
    polled = await run_phase(obs, "5 Hz", 2.0, "Card Cam", hz=5, width=640, clock=clock, sleep=clock.sleep)
    report = format_report([idle, polled], {"source": "Card Cam", "streaming": False, "recording": False})
    assert "idle" in report and "5 Hz" in report and "640x360" in report
    assert "GetSourceScreenshot failed (600): boom" in report
