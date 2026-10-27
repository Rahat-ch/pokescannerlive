"""The stream session: every card counted, with undo, totals and CSV export.

Stored in SQLite so a crash mid-stream doesn't lose the total. Undo voids a
scan rather than deleting it, so it can be restored and still shows in exports.
Repeats are expected: each scan counts, and `copy` says which copy it is.
"""

import csv
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TextIO

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    ended_at TEXT
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    added_at TEXT NOT NULL,
    name TEXT NOT NULL,
    set_name TEXT,
    number TEXT,
    variant TEXT,
    price_minor INTEGER NOT NULL,  -- smallest currency unit: cents, or yen
    currency TEXT NOT NULL,
    source TEXT NOT NULL,          -- 'manual' until identification lands
    card_id TEXT,                  -- catalog id, once cards are identified
    voided_at TEXT
);
"""

# Currencies with no minor unit; everything else is in hundredths.
ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW"}

CSV_FIELDS = ["added_at", "name", "set_name", "number", "variant", "price", "currency", "copy", "status", "source", "card_id"]


def minor_digits(currency: str) -> int:
    return 0 if currency in ZERO_DECIMAL_CURRENCIES else 2


def to_minor(price: str | Decimal, currency: str) -> int:
    """'12.50' USD -> 1250. Raises ValueError for anything that isn't a price."""
    try:
        amount = Decimal(str(price).strip().lstrip("$¥€£"))
    except InvalidOperation:
        raise ValueError(f"not a price: {price!r}") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError(f"not a price: {price!r}")
    minor = amount.scaleb(minor_digits(currency))
    if minor != minor.to_integral_value():
        raise ValueError(f"too many decimal places for {currency}: {price!r}")
    return int(minor)


def from_minor(minor: int, currency: str) -> Decimal:
    return Decimal(minor).scaleb(-minor_digits(currency))


def clean(text) -> str | None:
    """Stripped text, or None for missing or blank."""
    return (str(text).strip() or None) if text else None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Scan:
    id: int
    session_id: int
    added_at: str
    name: str
    set_name: str | None
    number: str | None
    variant: str | None
    price_minor: int
    currency: str
    source: str
    card_id: str | None
    voided_at: str | None
    copy: int = 1  # 1 for the first counted copy of this card this session, 2 for the next...

    @property
    def price(self) -> Decimal:
        return from_minor(self.price_minor, self.currency)

    @property
    def identity(self) -> tuple:
        """Two scans of the same card, for counting copies."""
        if self.card_id:
            return ("id", self.card_id)
        return ("text", self.name.casefold(), (self.set_name or "").casefold(),
                (self.number or "").casefold(), (self.variant or "").casefold())

    def to_json(self) -> dict:
        data = asdict(self)
        data["price"] = str(self.price)
        data["voided"] = self.voided_at is not None
        return data


class SessionStore:
    def __init__(self, path: Path | str, clock=utc_now):
        self.clock = clock
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.session_id = self._open_session()

    def close(self) -> None:
        self.db.close()

    def _open_session(self) -> int:
        """Resume the last unfinished session, e.g. after a crash; else start one."""
        row = self.db.execute("SELECT id FROM sessions WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            return row["id"]
        with self.db:
            return self.db.execute("INSERT INTO sessions (started_at) VALUES (?)", (self.clock(),)).lastrowid

    def new_session(self) -> int:
        with self.db:
            self.db.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", (self.clock(), self.session_id))
        self.session_id = self._open_session()
        return self.session_id

    def started_at(self) -> str:
        return self.db.execute("SELECT started_at FROM sessions WHERE id = ?", (self.session_id,)).fetchone()[0]

    def add(self, name: str, price: str | Decimal, currency: str = "USD", set_name: str | None = None,
            number: str | None = None, variant: str | None = None, source: str = "manual",
            card_id: str | None = None) -> Scan:
        name = name.strip()
        if not name:
            raise ValueError("a card needs a name")
        currency = currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError(f"not a currency code: {currency!r}")
        price_minor = to_minor(price, currency)
        with self.db:
            scan_id = self.db.execute(
                "INSERT INTO scans (session_id, added_at, name, set_name, number, variant, price_minor, currency, source, card_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.session_id, self.clock(), name, clean(set_name), clean(number), clean(variant),
                 price_minor, currency, source, clean(card_id)),
            ).lastrowid
        return self.get(scan_id)

    def get(self, scan_id: int) -> Scan | None:
        return next((s for s in self.scans() if s.id == scan_id), None)

    def scans(self) -> list[Scan]:
        """This session's scans, newest first, voided ones included, with copy numbers."""
        rows = self.db.execute("SELECT * FROM scans WHERE session_id = ? ORDER BY id", (self.session_id,)).fetchall()
        seen: dict[tuple, int] = {}
        scans = []
        for row in rows:
            scan = Scan(**dict(row))
            if scan.voided_at is None:
                seen[scan.identity] = seen.get(scan.identity, 0) + 1
                scan = Scan(**{**dict(row), "copy": seen[scan.identity]})
            scans.append(scan)
        return scans[::-1]

    def _set_voided(self, scan_id: int, voided_at: str | None) -> Scan | None:
        with self.db:
            changed = self.db.execute(
                "UPDATE scans SET voided_at = ? WHERE id = ? AND session_id = ?",
                (voided_at, scan_id, self.session_id),
            ).rowcount
        return self.get(scan_id) if changed else None

    def void(self, scan_id: int) -> Scan | None:
        return self._set_voided(scan_id, self.clock())

    def restore(self, scan_id: int) -> Scan | None:
        return self._set_voided(scan_id, None)

    def undo(self) -> Scan | None:
        """Void the most recent counted scan."""
        last = next((s for s in self.scans() if s.voided_at is None), None)
        return self.void(last.id) if last else None

    def counted(self) -> list[Scan]:
        return [s for s in self.scans() if s.voided_at is None]

    def totals(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for scan in self.counted():
            totals[scan.currency] = totals.get(scan.currency, Decimal(0)) + scan.price
        return totals

    def state(self, recent: int = 50) -> dict:
        """Everything the overlay and control panel show."""
        scans = self.scans()
        counted = [s for s in scans if s.voided_at is None]
        return {
            "session": {"id": self.session_id, "started_at": self.started_at()},
            "totals": [{"currency": c, "amount": str(a)} for c, a in sorted(self.totals().items())],
            "count": len(counted),
            "last": counted[0].to_json() if counted else None,
            "scans": [s.to_json() for s in scans[:recent]],
        }

    def export_csv(self, out: TextIO) -> None:
        writer = csv.DictWriter(out, CSV_FIELDS)
        writer.writeheader()
        for scan in reversed(self.scans()):
            writer.writerow({
                "added_at": scan.added_at, "name": scan.name, "set_name": scan.set_name or "",
                "number": scan.number or "", "variant": scan.variant or "", "price": str(scan.price),
                "currency": scan.currency, "copy": scan.copy if scan.voided_at is None else "",
                "status": "voided" if scan.voided_at else "counted", "source": scan.source,
                "card_id": scan.card_id or "",
            })
