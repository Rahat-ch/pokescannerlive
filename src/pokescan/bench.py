"""Milestone 1: how fast is GetSourceScreenshot, and what does polling it cost OBS?

Each phase polls one source at a target rate with one request in flight (never
queued), and compares OBS's render/output frame counters against an idle phase
of the same length.
"""

import asyncio
import math
import statistics
import struct
import time
from dataclasses import dataclass, field

from pokescan.obs import ObsError


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; nan for no values."""
    if not values:
        return math.nan
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from a JPEG's SOF marker, or None if it isn't one."""
    if data[:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        # SOF0..SOF15, minus DHT (C4), JPG (C8) and DAC (CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


@dataclass
class PhaseResult:
    label: str
    target_hz: float | None
    width: int | None
    seconds: float
    latencies_ms: list[float] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    frame_bytes: list[int] = field(default_factory=list)
    frame_size: tuple[int, int] | None = None
    render_ms_samples: list[float] = field(default_factory=list)
    render_skipped: int = 0
    render_total: int = 0
    output_skipped: int = 0
    output_total: int = 0

    @property
    def achieved_hz(self) -> float:
        return len(self.latencies_ms) / self.seconds if self.seconds else 0.0

    @property
    def render_skipped_pct(self) -> float:
        return 100 * self.render_skipped / self.render_total if self.render_total else 0.0

    @property
    def output_skipped_pct(self) -> float:
        return 100 * self.output_skipped / self.output_total if self.output_total else 0.0

    @property
    def mean_render_ms(self) -> float:
        return statistics.fmean(self.render_ms_samples) if self.render_ms_samples else math.nan

    def add_counters(self, first: dict, last: dict) -> None:
        self.render_skipped = last["renderSkippedFrames"] - first["renderSkippedFrames"]
        self.render_total = last["renderTotalFrames"] - first["renderTotalFrames"]
        self.output_skipped = last["outputSkippedFrames"] - first["outputSkippedFrames"]
        self.output_total = last["outputTotalFrames"] - first["outputTotalFrames"]


async def run_phase(
    obs,
    label: str,
    seconds: float,
    source: str | None = None,
    hz: float | None = None,
    width: int | None = None,
    stats_every: float = 1.0,
    clock=time.monotonic,
    sleep=asyncio.sleep,
) -> PhaseResult:
    """Poll `source` at `hz` for `seconds`; with no `hz`, just watch OBS idle."""
    result = PhaseResult(label, hz, width, seconds)
    first = await obs.stats()
    result.render_ms_samples.append(first["averageFrameRenderTime"])

    start = clock()
    end = start + seconds
    next_shot = start
    next_stats = start + stats_every
    while (now := clock()) < end:
        if now >= next_stats:
            result.render_ms_samples.append((await obs.stats())["averageFrameRenderTime"])
            next_stats += stats_every
        elif hz and now >= next_shot:
            t0 = clock()
            try:
                frame = await obs.screenshot(source, width=width)
            except ObsError as e:
                result.errors.append(str(e))
            else:
                result.latencies_ms.append((clock() - t0) * 1000)
                result.frame_bytes.append(len(frame))
                result.frame_size = result.frame_size or jpeg_size(frame)
            # Fell behind? Start the next one now rather than bursting to catch up.
            next_shot = max(next_shot + 1 / hz, clock())
        else:
            wake = min(next_stats, next_shot if hz else end, end)
            await sleep(max(0.0, wake - clock()))

    result.seconds = clock() - start
    result.add_counters(first, await obs.stats())
    return result


async def full_res_shots(obs, source: str, count: int, clock=time.monotonic) -> PhaseResult:
    """`count` back-to-back native-resolution screenshots: the per-scan "fire" cost."""
    result = PhaseResult("full-res", None, None, 0.0)
    first = await obs.stats()
    start = clock()
    for _ in range(count):
        t0 = clock()
        try:
            frame = await obs.screenshot(source)
        except ObsError as e:
            result.errors.append(str(e))
            continue
        result.latencies_ms.append((clock() - t0) * 1000)
        result.frame_bytes.append(len(frame))
        result.frame_size = result.frame_size or jpeg_size(frame)
    result.seconds = clock() - start
    result.add_counters(first, await obs.stats())
    return result


def format_report(results: list[PhaseResult], context: dict) -> str:
    lines = [
        f"OBS {context.get('obs_version', '?')}, obs-websocket {context.get('ws_version', '?')}, "
        f"source {context.get('source')!r}",
        f"streaming: {context.get('streaming')}, recording: {context.get('recording')}"
        + ("" if context.get("streaming") or context.get("recording") else
           "  (output-skipped frames only count while streaming or recording)"),
        "",
        f"{'phase':<10} {'target':>7} {'got':>6} {'p50 ms':>7} {'p95 ms':>7} {'max ms':>7} "
        f"{'frame':>11} {'KB':>5} {'render ms':>9} {'render skip':>11} {'output skip':>11} {'errors':>6}",
    ]
    for r in results:
        size = f"{r.frame_size[0]}x{r.frame_size[1]}" if r.frame_size else "-"
        kb = f"{statistics.fmean(r.frame_bytes) / 1024:.0f}" if r.frame_bytes else "-"
        target = f"{r.target_hz:g} Hz" if r.target_hz else "-"
        got = f"{r.achieved_hz:.1f}" if r.latencies_ms else "-"
        lines.append(
            f"{r.label:<10} {target:>7} {got:>6} {percentile(r.latencies_ms, 50):>7.1f} "
            f"{percentile(r.latencies_ms, 95):>7.1f} {max(r.latencies_ms, default=math.nan):>7.1f} "
            f"{size:>11} {kb:>5} {r.mean_render_ms:>9.2f} "
            f"{r.render_skipped:>5} {r.render_skipped_pct:>4.1f}% {r.output_skipped:>5} {r.output_skipped_pct:>4.1f}% "
            f"{len(r.errors):>6}"
        )
    errors = sorted({e for r in results for e in r.errors})
    if errors:
        lines += ["", "errors:"] + [f"  {e}" for e in errors]
    return "\n".join(lines)
