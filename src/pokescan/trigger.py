"""When to scan a card: the whole policy, with no camera, OBS or network in it.

The scanner feeds it each poll's card outline (`observe`) and, when asked, a
sharp full-resolution look at the card (`judge`). Time is passed in, so every
rule is testable offline. The shape follows Cardstream's DecisionCore.

Rules:
- A card must be held steady (corners moving slower than `still_speed` card
  heights per second) for `still_seconds` before we look closely. A hand-held
  card sways 5-10% of its height every half second, so "steady" is a speed,
  not zero movement; motion blur is caught by the sharpness check instead.
- A blurry crop holds the scan rather than failing it, and goes anyway after
  `send_anyway_after` seconds, so a soft camera can't block scanning forever.
- One scan per presentation. The card's fingerprint is committed when the scan
  fires, not when a result comes back, so a card that can't be named isn't
  re-sent every poll.
- While the card stays up, a new scan fires only after `confirm_changes`
  consecutive "different card" verdicts: a swap without lowering the hand.
- The card is gone once no outline has been seen for `gone_after` seconds;
  the next card, or the same one shown again, is a new presentation. Repeats
  count, so showing a card again scans it again.
- At least `cooldown` seconds between scans, as a cost safety net.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import numpy as np


class Verdict(Enum):
    FIRE = "fire"      # scan this crop
    HOLD = "hold"      # blurry: wait for a sharper look
    SAME = "same"      # the card already scanned
    WAIT = "wait"      # maybe a new card: confirm on the next look


class Status(Enum):
    NO_CARD = "no card"
    MOVING = "card moving"
    STEADY = "card held"
    FOCUSING = "waiting for focus"
    SCANNED = "scanned"


@dataclass
class Trigger:
    compare: Callable[[Any, Any], str]  # (committed, new) fingerprint -> 'same' | 'different' | 'unsure'
    still_seconds: float = 0.6
    still_speed: float = 0.25
    min_sharpness: float = 0.68
    send_anyway_after: float = 3.0
    gone_after: float = 1.0
    cooldown: float = 1.0
    check_every: float = 0.4
    confirm_changes: int = 2

    present: bool = field(default=False, init=False)
    status: Status = field(default=Status.NO_CARD, init=False)
    _quad: np.ndarray | None = field(default=None, init=False)
    _last_seen: float = field(default=float("-inf"), init=False)
    _still_since: float = field(default=0.0, init=False)
    _next_check: float = field(default=float("-inf"), init=False)
    _last_fire: float = field(default=float("-inf"), init=False)
    _blurry_since: float | None = field(default=None, init=False)
    _committed: Any = field(default=None, init=False)
    _changes: int = field(default=0, init=False)

    def observe(self, now: float, quad: np.ndarray | None) -> bool:
        """One poll's card outline (or None). True means: take a sharp look now."""
        if quad is None:
            if self.present and now - self._last_seen >= self.gone_after:
                self._card_left()
            return False

        quad = np.asarray(quad, np.float32)
        if not self.present:
            self.present = True
            self._still_since = now
        elif self._moved(quad, now - self._last_seen):
            self._still_since = now
            self._blurry_since = None
        self._quad, self._last_seen = quad, now

        still = now - self._still_since >= self.still_seconds
        if not still:
            self.status = Status.MOVING
            return False
        if self._committed is not None:
            self.status = Status.SCANNED
        elif self._blurry_since is None:
            self.status = Status.STEADY
        return now >= self._next_check and now - self._last_fire >= self.cooldown

    def judge(self, now: float, sharpness: float, fingerprint: Any) -> Verdict:
        """The sharp look `observe` asked for: its sharpness and fingerprint."""
        self._next_check = now + self.check_every
        if sharpness < self.min_sharpness:
            if self._blurry_since is None:
                self._blurry_since = now
            if now - self._blurry_since < self.send_anyway_after:
                if self._committed is None:
                    self.status = Status.FOCUSING
                return Verdict.HOLD
        self._blurry_since = None

        if self._committed is None:
            return self._fire(now, fingerprint)
        verdict = self.compare(self._committed, fingerprint)
        if verdict == "same":
            self._changes = 0
            return Verdict.SAME
        if verdict == "different":
            self._changes += 1
            if self._changes >= self.confirm_changes:
                return self._fire(now, fingerprint)
        return Verdict.WAIT

    def _fire(self, now: float, fingerprint: Any) -> Verdict:
        self._committed = fingerprint
        self._last_fire = now
        self._changes = 0
        self.status = Status.SCANNED
        return Verdict.FIRE

    def _moved(self, quad: np.ndarray, elapsed: float) -> bool:
        height = float(np.linalg.norm(quad[3] - quad[0])) or 1.0
        return float(np.abs(quad - self._quad).max()) > self.still_speed * height * max(elapsed, 1e-3)

    def _card_left(self) -> None:
        self.present = False
        self.status = Status.NO_CARD
        self._quad = None
        self._committed = None
        self._changes = 0
        self._blurry_since = None
