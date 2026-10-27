"""From a scanned crop to a named, priced card.

1. Read the name, HP and collector number on this Mac (`ocr`). The name goes
   on stream straight away.
2. Match against Scrydex's cards: the stream's set if one is chosen (fetched
   once a day, then free), plus a name search (1 credit, cached). Name and HP
   find the card; comparing the scan's artwork with each candidate's reference
   image picks the exact printing (on real scans: 41-255 matching points for
   the right printing, at most 14 for any other).
3. One clear match with one priced variant is added to the total at once.
   One clear match with several variants waits for a one-click variant pick.
   No clear match offers the top three cards. Nothing found, or Scrydex
   unreachable, falls back to typing it in.

A rescan of the same card while it was never lowered (the trigger's rare
false "different card") is dropped instead of counted twice.
"""

import asyncio
import logging
import re
import urllib.request
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from pokescan import ocr
from pokescan.detect import fingerprint, matching_points
from pokescan.ocr import CardText
from pokescan.scanner import Capture, Scanner
from pokescan.scrydex import Scrydex, ScrydexError, quote, summary
from pokescan.session import Scan, SessionStore

log = logging.getLogger("pokescan.identify")

# Scoring weights and thresholds, set on 14 real stream crops; retune as scans accumulate.
HP_MATCH, HP_MISMATCH = 0.25, -0.3
# The collector number is tiny and often misread by a digit ("042/064" for 062/084),
# so a near miss still counts, and the number only counts if the set size is close.
NUMBER_MATCH, NUMBER_NEAR = 0.2, 0.1
TOTAL_MATCH, TOTAL_NEAR = 0.15, 0.08
STREAM_SET = 0.3
# Physical English cards unless the stream's set says otherwise; TCG Pocket sets are online only.
PHYSICAL_ENGLISH = "language_code:EN -expansion.is_online_only:true"
CONFIDENT, MARGIN = 1.0, 0.2
ART_WEIGHT, ART_FULL_AT = 0.6, 100  # matching artwork points worth up to +0.6, in full at 100
ART_CANDIDATES, ART_WINDOW = 5, 0.35  # compare art for up to 5 candidates within 0.35 of the best
CARD_ID = re.compile(r"^[A-Za-z0-9._-]+$")
SUFFIXES = {"ex", "gx", "v", "vmax", "vstar", "lv", "x", "break", "prime", "star"}


def normalise(text: str) -> str:
    text = text.lower().replace("é", "e").replace("’", "'")
    return " ".join(re.findall(r"[a-z0-9']+", text))


def name_similarity(read: str, actual: str) -> float:
    """0..1. Whole-string similarity, or the share of the card's name words read right.

    The second catches garbled leading words: "merton tal ogerpon ex" for
    "cornerstone mask ogerpon ex" still has its distinctive word right.
    """
    a, b = normalise(read), normalise(actual)
    whole = SequenceMatcher(None, a, b).ratio()
    actual_words = [w for w in b.split() if w not in SUFFIXES and len(w) > 2]
    if not actual_words:
        return whole
    read_words = a.split()
    hits = sum(max((SequenceMatcher(None, w, r).ratio() for r in read_words), default=0) >= 0.85 for w in actual_words)
    return max(whole, 0.9 * hits / len(actual_words) if hits else 0)


def digits_off(read: str, actual) -> int | None:
    """How many digits differ between a read number and the real one, both as 3 digits."""
    actual = str(actual or "").split("/")[0]
    if not read or not actual.isdigit() or len(actual) > 3:
        return None
    return sum(a != b for a, b in zip(read.zfill(3), actual.zfill(3)))


def score(card: dict, text: CardText, stream_set: str | None) -> float:
    s = name_similarity(text.name, card.get("name", "")) if text.name else 0.0
    if text.hp and card.get("hp"):
        s += HP_MATCH if str(text.hp) == str(card["hp"]) else HP_MISMATCH
    total_off = digits_off(text.total, (card.get("expansion") or {}).get("printed_total"))
    if total_off is not None and total_off <= 1:
        s += TOTAL_MATCH if total_off == 0 else TOTAL_NEAR
        number_off = digits_off(text.number, card.get("number"))
        if number_off is not None and number_off <= 1:
            s += NUMBER_MATCH if number_off == 0 else NUMBER_NEAR
    if stream_set and (card.get("expansion") or {}).get("id") == stream_set:
        s += STREAM_SET
    return s


def rank(cards: list[dict], text: CardText, stream_set: str | None) -> list[tuple[float, dict]]:
    unique = {c["id"]: c for c in cards if c.get("id")}
    return sorted(((score(c, text, stream_set), c) for c in unique.values()), key=lambda p: -p[0])


def art_bonus(points: int) -> float:
    return ART_WEIGHT * min(points, ART_FULL_AT) / ART_FULL_AT


def is_confident(ranked: list[tuple[float, dict]]) -> bool:
    if not ranked or ranked[0][0] < CONFIDENT:
        return False
    return len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= MARGIN


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pokescan"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def name_queries(name: str, hp: int | None = None) -> list[str]:
    """The exact name first, then its distinctive words for garbled reads; narrowed by HP when read."""
    words = sorted({w for w in re.findall(r"[A-Za-z']{4,}", name) if w.lower() not in SUFFIXES}, key=len, reverse=True)
    names = [quote(name)] + [quote(w) for w in words[:2] if w.lower() != name.lower()]
    narrow = f" hp:{hp}" if hp else ""
    return [f"name:{n}{narrow} {PHYSICAL_ENGLISH}" for n in names]


class Identifier:
    def __init__(self, scrydex: Scrydex, store: SessionStore, scanner: Scanner, crops_dir: Path,
                 stream_set: Callable[[], str | None] = lambda: None, on_added=lambda scan: None,
                 read_card: Callable[[Path], CardText] = ocr.read_card, images_dir: Path | None = None,
                 fetch_image: Callable[[str], bytes] | None = None):
        self.scrydex = scrydex
        self.store = store
        self.scanner = scanner
        self.crops_dir = crops_dir
        self.stream_set = stream_set
        self.on_added = on_added
        self.read_card = read_card
        self.images_dir = images_dir
        self.fetch_image = fetch_image or download
        self._cards: dict[str, dict] = {}  # every card seen this run, for picks without another lookup
        self._last: tuple[int, str] | None = None  # (presentation, card id) of the last card added
        self._tasks: set[asyncio.Task] = set()
        scanner.on_capture = self.submit

    def submit(self, capture: Capture) -> None:
        task = asyncio.get_running_loop().create_task(self.identify(capture))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def identify(self, capture: Capture) -> None:
        text = await asyncio.to_thread(self.read_card, self.crops_dir / capture.crop)
        log.info("read %s: name=%r hp=%s number=%s/%s", capture.crop, text.name, text.hp, text.number, text.total)
        self.scanner.update(capture, name=text.name)
        if not text.name:
            self.scanner.update(capture, state="unknown", message="Couldn't read the name. Type the card in.")
            return
        try:
            ranked = rank(await self._pool(text), text, self.stream_set())
            ranked = await self._compare_art(capture, ranked)
        except ScrydexError as e:
            self.scanner.update(capture, state="unknown", message=f"{e} Type the card in.")
            return
        if not ranked or ranked[0][0] < 0.5:
            self.scanner.update(capture, state="unknown", message=f"No card called {text.name!r} found. Type it in.")
            return

        if not is_confident(ranked):
            self.scanner.update(capture, state="pick card", candidates=[summary(c) for _, c in ranked[:3]],
                                message="Which one is it?")
            return
        card = summary(ranked[0][1])
        if self._last == (capture.presentation, card["card_id"]):
            log.info("dropped %s: same card as the last one, never lowered", capture.crop)
            self.scanner.dismiss(capture.id)
            return
        priced = [v for v in card["variants"] if v["price"] is not None]
        if len(priced) == 1:
            self._add(capture, card, priced[0])
        elif priced:
            self.scanner.update(capture, state="pick variant", name=card["name"], card=card)
        else:
            self.scanner.update(capture, state="unknown", name=card["name"], card=card,
                                message="Scrydex has no near-mint price for it. Type the price in.")

    async def choose(self, capture_id: int, card_id: str, variant: str) -> Scan:
        """The streamer picked a card and variant in the dock."""
        capture = self.scanner.get(capture_id)
        if capture is None:
            raise LookupError("no such scan")
        raw = self._cards.get(card_id) or await self.scrydex.card(card_id)
        card = summary(raw)
        chosen = next((v for v in card["variants"] if v["name"] == variant and v["price"] is not None), None)
        if chosen is None:
            raise ValueError("no near-mint price for that variant. Type the price in.")
        return self._add(capture, card, chosen)

    async def search(self, text: str) -> list[dict]:
        """Cards for a name typed into the dock, best first (the stream's set ranked up)."""
        if not text.strip():
            return []
        cards = []
        for query in name_queries(text):
            if cards := await self.scrydex.search(query):
                break
        ranked = rank(self._remember(cards), CardText(text, None, None, None), self.stream_set())
        return [summary(c) for _, c in ranked[:8]]

    async def _pool(self, text: CardText) -> list[dict]:
        cards = []
        if set_id := self.stream_set():
            cards += await self.scrydex.expansion_cards(set_id)
            if is_confident(rank(cards, text, set_id)):
                return self._remember(cards)  # matched within the set: no search needed
        for query in name_queries(text.name, text.hp) + (name_queries(text.name) if text.hp else []):
            found = await self.scrydex.search(query)
            cards += found
            if found:
                break
        return self._remember(cards)

    async def _compare_art(self, capture: Capture, ranked: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
        """Add how well the scan's artwork matches each close candidate's reference image."""
        if not ranked or self.images_dir is None:
            return ranked
        close = [(s, c) for s, c in ranked[:ART_CANDIDATES] if s >= ranked[0][0] - ART_WINDOW]
        crop = await asyncio.to_thread(cv2.imread, str(self.crops_dir / capture.crop))
        if crop is None:
            return ranked
        scan_print = await asyncio.to_thread(fingerprint, crop)
        prints = await asyncio.gather(*(self._reference_print(c) for _, c in close))
        rescored = [(s + (art_bonus(matching_points(scan_print, p)) if p else 0), c) for (s, c), p in zip(close, prints)]
        return sorted(rescored, key=lambda p: -p[0]) + ranked[len(close):]

    async def _reference_print(self, card: dict):
        """Fingerprint of a card's reference image, downloaded once (no credits) and kept on disk."""
        url = ((card.get("images") or [{}])[0]).get("medium")
        if not url or not CARD_ID.match(card.get("id", "")):
            return None
        path = self.images_dir / f"{card['id']}.jpg"
        try:
            if not path.exists():
                data = await asyncio.to_thread(self.fetch_image, url)
                self.images_dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            image = cv2.imdecode(np.frombuffer(path.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        except OSError as e:
            log.warning("reference image for %s: %s", card["id"], e)
            return None
        return await asyncio.to_thread(fingerprint, image) if image is not None else None

    def _remember(self, cards: list[dict]) -> list[dict]:
        self._cards.update((c["id"], c) for c in cards if c.get("id"))
        return cards

    def _add(self, capture: Capture, card: dict, variant: dict) -> Scan:
        scan = self.store.add(
            name=card["name"], price=Decimal(variant["price"]), currency=variant["currency"] or "USD",
            set_name=card["set_name"], number=card["number"], variant=variant["label"],
            source="scanner", card_id=card["card_id"], crop=capture.crop,
        )
        self._last = (capture.presentation, card["card_id"])
        self.scanner.take(capture.crop)
        self.on_added(scan)
        log.info("added %s (%s) %s %s", scan.name, scan.variant, scan.price, scan.currency)
        return scan

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.scrydex.close()
