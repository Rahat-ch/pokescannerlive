"""The live scanner: poll OBS, find the card, decide when to scan, save the crop.

Polls a small screenshot for detection and stillness, then takes one
full-resolution screenshot for the crop that gets identified ("analyse small,
identify big"). One request in flight, never queued: each poll uses the newest
frame. Measured cost to OBS is negligible (research/obs-card-scanner.md §5.4).
"""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from pokescan.config import ObsSettings
from pokescan.detect import CardDetector, Detection, compare, fingerprint, sharpness, warp
from pokescan.obs import Obs, ObsError
from pokescan.trigger import Trigger, Verdict

log = logging.getLogger("pokescan.scanner")

POLL_WIDTH = 960
POLL_HZ_IDLE = 2.0
POLL_HZ_CARD = 5.0


def decode(jpeg: bytes) -> np.ndarray:
    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("OBS sent an image that isn't a JPEG")
    return frame


@dataclass
class Look:
    crop: np.ndarray
    sharpness: float
    confidence: float


def close_look(detector: CardDetector, full: np.ndarray, polled: Detection) -> Look:
    """Find the card again in the full-resolution frame and cut it out.

    The card may have shifted a little since the poll; if the detector misses
    it at full size, fall back to the poll's outline scaled up.
    """
    height, width = full.shape[:2]
    found = detector.detect(full) or polled.scaled(width, height)
    crop = warp(full, found.quad)
    return Look(crop, sharpness(crop), found.confidence)


@dataclass
class Capture:
    """A scanned card on its way to the total."""
    id: int
    at: str
    crop: str  # file name under the crops directory
    confidence: float
    sharpness: float
    presentation: int = 0  # which time a card was brought into frame; same number = never lowered
    # "manual": no identifier, type it in. "identifying": reading and looking it up.
    # "pick variant": card known, choose its printing. "pick card": choose from `candidates`.
    # "unknown": not identified, type it in.
    state: str = "manual"
    name: str | None = None        # best reading so far, shown on stream
    card: dict | None = None       # the matched card, for "pick variant"
    candidates: list = field(default_factory=list)
    message: str | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "at": self.at, "crop": self.crop,
                "confidence": round(self.confidence, 3), "sharpness": round(self.sharpness, 3),
                "state": self.state, "name": self.name, "card": self.card,
                "candidates": self.candidates, "message": self.message}


class Scanner:
    def __init__(self, settings: ObsSettings, source: str, detector: CardDetector, crops_dir: Path,
                 on_change: Callable[[], None] = lambda: None):
        self.settings = settings
        self.source = source
        self.detector = detector
        self.crops_dir = crops_dir
        self.on_change = on_change
        self.on_capture: Callable[[Capture], None] | None = None  # set when cards are identified
        self.trigger = Trigger(compare=compare)
        self.captures: deque[Capture] = deque(maxlen=20)
        self.connected = False
        self.problem: str | None = None
        self._next_id = 1
        self._last_snapshot = None

    def snapshot(self) -> dict:
        return {
            "source": self.source,
            "connected": self.connected,
            "problem": self.problem,
            "status": self.trigger.status.value,
            "captures": [c.to_json() for c in reversed(self.captures)],
        }

    def dismiss(self, capture_id: int) -> bool:
        before = len(self.captures)
        self.captures = deque((c for c in self.captures if c.id != capture_id), maxlen=self.captures.maxlen)
        changed = len(self.captures) != before
        if changed:
            self._changed()
        return changed

    def get(self, capture_id: int) -> Capture | None:
        return next((c for c in self.captures if c.id == capture_id), None)

    def update(self, capture: Capture, **fields) -> None:
        for name, value in fields.items():
            setattr(capture, name, value)
        self._changed()

    def take(self, crop_name: str) -> None:
        """A capture was used for a scan: drop it from the pending list."""
        self.captures = deque((c for c in self.captures if c.crop != crop_name), maxlen=self.captures.maxlen)
        self._changed()

    def _changed(self, force: bool = True) -> None:
        snap = self.snapshot()
        if force or snap != self._last_snapshot:
            self._last_snapshot = snap
            self.on_change()

    async def run(self) -> None:
        """Poll forever, reconnecting to OBS as needed."""
        backoff = 1.0
        while True:
            try:
                async with Obs(self.settings) as obs:
                    self.connected, self.problem, backoff = True, None, 1.0
                    self._changed()
                    await self._poll(obs)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # OBS closed, camera off, socket dropped: report and retry
                log.warning("scanner: %s", e)
                self.connected, self.problem = False, str(e)
                self._changed(force=False)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 10.0)

    async def _poll(self, obs: Obs) -> None:
        while True:
            started = time.monotonic()
            try:
                small = decode(await obs.screenshot(self.source, width=POLL_WIDTH, quality=80))
            except ObsError as e:
                if "(702)" not in str(e):
                    raise
                self.problem = f"OBS has no video from {self.source!r}. Is the camera on?"
                self._changed(force=False)
                await asyncio.sleep(2.0)
                continue
            self.problem = None
            found = await asyncio.to_thread(self.detector.detect, small)
            now = time.monotonic()
            if self.trigger.observe(now, found.quad if found else None):
                await self._look(obs, now, found)
            self._changed(force=False)
            hz = POLL_HZ_CARD if self.trigger.present else POLL_HZ_IDLE
            await asyncio.sleep(max(0.0, started + 1 / hz - time.monotonic()))

    async def _look(self, obs: Obs, now: float, polled: Detection) -> None:
        full = decode(await obs.screenshot(self.source, quality=92))
        look = await asyncio.to_thread(close_look, self.detector, full, polled)
        card_print = await asyncio.to_thread(fingerprint, look.crop)
        if self.trigger.judge(now, look.sharpness, card_print) is Verdict.FIRE:
            self._save(look)

    def _save(self, look: Look) -> None:
        at = datetime.now(timezone.utc)
        name = f"{at:%Y%m%d-%H%M%S}-{self._next_id:04d}.jpg"
        self.crops_dir.mkdir(parents=True, exist_ok=True)
        # Saved before anything else uses it, so every scan is kept for review and testing.
        cv2.imwrite(str(self.crops_dir / name), look.crop, [cv2.IMWRITE_JPEG_QUALITY, 92])
        capture = Capture(self._next_id, at.isoformat(timespec="seconds"), name, look.confidence,
                          look.sharpness, self.trigger.presentation,
                          state="identifying" if self.on_capture else "manual")
        self.captures.append(capture)
        self._next_id += 1
        log.info("scanned card %s (sharpness %.2f)", name, look.sharpness)
        self._changed()
        if self.on_capture:
            self.on_capture(capture)


def replay(detector: CardDetector, frames: list[Path], hz: float, poll_width: int = POLL_WIDTH):
    """Run the scanner's decisions over saved frames, as if polled live at `hz`.

    Yields (frame index, status, verdict or None) per frame, and the crop on FIRE.
    """
    trigger = Trigger(compare=compare)
    for i, path in enumerate(frames):
        full = cv2.imread(str(path))
        height, width = full.shape[:2]
        small = cv2.resize(full, (poll_width, round(height * poll_width / width)))
        found = detector.detect(small)
        now = i / hz
        verdict, crop = None, None
        if trigger.observe(now, found.quad if found else None):
            look = close_look(detector, full, found)
            verdict = trigger.judge(now, look.sharpness, fingerprint(look.crop))
            crop = look.crop if verdict is Verdict.FIRE else None
        yield i, trigger.status.value, verdict, crop
