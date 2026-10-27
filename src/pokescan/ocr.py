"""Read a card's name, HP and collector number from its upright crop, on this Mac.

Uses macOS's built-in text recognition (Vision framework, via PyObjC), so
nothing leaves the machine. On real stream crops it read plain names exactly
(Pinsir, Bastiodon, Black Belt's Training...), HP every time, and the collector
number about half the time; long names can come out garbled ("Comentione Mai
Ozerpon eX"), which `identify` handles by fuzzy matching against the catalog.
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Words in the top strip that aren't the card's name.
NOT_NAMES = re.compile(r"^(basic|stage\s*\d?|supporter|trainer|item|stadium|tool|pok[eé]mon tool|tera|"
                       r"special energy|energy|ace spec|evolves from.*)$", re.I)
HP = re.compile(r"(\d{2,3})\D{0,3}$")  # the type symbol after it can read as "€" or "©"
NUMBER = re.compile(r"(\d{1,3})\s*/\s*(\d{2,3})")
# Common misreads in the tiny collector number.
DIGITS = str.maketrans({"O": "0", "o": "0", "о": "0", "О": "0", "D": "0", "I": "1", "l": "1", "S": "5", "B": "8", "к": "4"})


@dataclass(frozen=True)
class Line:
    text: str
    top: float     # 0 at the top of the card, 1 at the bottom
    left: float
    height: float
    confidence: float


@dataclass(frozen=True)
class CardText:
    name: str | None
    hp: int | None
    number: str | None   # "070"
    total: str | None    # "084"
    lines: tuple[Line, ...] = field(default=(), repr=False)


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import Vision  # noqa: F401
    except ImportError:
        return False
    return True


def recognise(path: Path) -> list[Line]:
    """Every line of text macOS finds in the image."""
    import Vision
    from Foundation import NSData

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    request.setUsesLanguageCorrection_(False)  # card names aren't dictionary words
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(NSData.dataWithContentsOfFile_(str(path)), None)
    handler.performRequests_error_([request], None)
    lines = []
    for observation in request.results() or []:
        best = observation.topCandidates_(1)[0]
        box = observation.boundingBox()  # normalised, origin at the bottom left
        lines.append(Line(str(best.string()), 1 - box.origin.y - box.size.height, box.origin.x,
                          box.size.height, float(best.confidence())))
    return lines


def parse(lines: list[Line]) -> CardText:
    """Pick the name, HP and number out of the recognised lines by where they sit on the card."""
    top = [l for l in lines if l.top < 0.13]
    names = [l for l in top if l.left < 0.6 and not NOT_NAMES.match(l.text.strip()) and re.search(r"[A-Za-z]{3}", l.text)]
    name = max(names, key=lambda l: l.height).text.strip() if names else None

    hp = None
    for l in sorted(top, key=lambda l: -l.left):
        if l.left > 0.55 and (m := HP.search(l.text)):
            hp = int(m.group(1))
            break

    number = total = None
    for l in sorted((l for l in lines if l.top > 0.85), key=lambda l: -l.confidence):
        if m := NUMBER.search(l.text.translate(DIGITS)):
            number, total = m.group(1).zfill(3), m.group(2).zfill(3)
            break
    return CardText(name, hp, number, total, tuple(lines))


def read_card(path: Path) -> CardText:
    return parse(recognise(path))
