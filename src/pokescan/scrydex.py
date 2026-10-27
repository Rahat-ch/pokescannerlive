"""Scrydex: find cards and their prices.

Docs: https://scrydex.com/docs/pokemon/cards and /docs/getting-started/prices.
Each request costs 1 credit and returns cards with every variant's prices
(`include=prices`). Prices change at most daily and should be cached for 24 h,
which `Cache` does across streams, so a card seen again costs nothing.

Response shapes were checked against live calls on 2026-10-27; the tests use
hand-made cards of the same shape rather than Scrydex's data.
"""

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import aiohttp

API = "https://api.scrydex.com/pokemon/v1"
CACHE_TTL = 24 * 3600


class ScrydexError(Exception):
    pass


@dataclass(frozen=True)
class Price:
    amount: Decimal
    currency: str


_WORDS = re.compile(r"[A-Z]?[a-z]+|[0-9]+(?:st|nd|rd|th)?|[A-Z]+(?![a-z])")
_LABELS = {"holofoil": "Holo", "reverse": "Reverse", "first": "1st", "edition": "Edition"}


def variant_label(name: str) -> str:
    """'unlimitedHolofoil' -> 'Holo', 'reverseHolofoil' -> 'Reverse Holo', 'firstEditionHolofoil' -> '1st Edition Holo'."""
    words = [w.lower() for w in _WORDS.findall(name)]
    if words[:1] == ["unlimited"] and len(words) > 1:
        words = words[1:]
    return " ".join(_LABELS.get(w, w.capitalize()) for w in words) or name


def near_mint(variant: dict) -> Price | None:
    """A variant's raw near-mint market price."""
    for price in variant.get("prices") or []:
        if price.get("type") == "raw" and price.get("condition") == "NM" and price.get("market") is not None:
            return Price(Decimal(str(price["market"])), price.get("currency") or "USD")
    return None


# Trainer subtypes, in the order they're checked: an ACE SPEC Item is an item.
TRAINER_SOUNDS = (("supporter", "supporter"), ("stadium", "stadium"), ("tool", "item"), ("item", "item"))


def sound(card: dict) -> str | None:
    """What plays on stream for a card: its Pokémon's cry ('cry:151'), else a sound for its kind."""
    supertype = (card.get("supertype") or "").casefold()
    if supertype.startswith("pok") and (dex := next(iter(card.get("national_pokedex_numbers") or []), None)):
        return f"cry:{int(dex)}"
    if supertype == "energy":
        return "energy"
    subtypes = " ".join(card.get("subtypes") or []).casefold()
    return next((name for word, name in TRAINER_SOUNDS if word in subtypes), "item" if supertype == "trainer" else None)


def summary(card: dict) -> dict:
    """What the dock and overlay need to show a card and choose a variant."""
    images = card.get("images") or [{}]
    variants = []
    for v in card.get("variants") or []:
        price = near_mint(v)
        variants.append({"name": v["name"], "label": variant_label(v["name"]),
                         "price": str(price.amount) if price else None,
                         "currency": price.currency if price else None})
    return {
        "card_id": card["id"],
        "name": card.get("name") or card["id"],
        "set_name": (card.get("expansion") or {}).get("name"),
        "number": card.get("printed_number") or card.get("number"),
        "hp": card.get("hp"),
        "image": images[0].get("small"),
        "sound": sound(card),
        "variants": variants,
    }


class Cache:
    """Scrydex responses kept for 24 h in SQLite."""

    def __init__(self, path: Path, ttl: float = CACHE_TTL, clock=time.time):
        self.ttl, self.clock = ttl, clock
        self._db = sqlite3.connect(path)
        self._db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, fetched_at REAL, body TEXT)")

    def get(self, key: str):
        row = self._db.execute("SELECT fetched_at, body FROM responses WHERE key = ?", (key,)).fetchone()
        return json.loads(row[1]) if row and self.clock() - row[0] < self.ttl else None

    def put(self, key: str, value) -> None:
        with self._db:
            self._db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?)", (key, self.clock(), json.dumps(value)))

    def close(self) -> None:
        self._db.close()


def quote(text: str) -> str:
    """A phrase for Scrydex's Lucene-style query."""
    return '"' + re.sub(r'["\\]', " ", text).strip() + '"'


class Scrydex:
    def __init__(self, api_key: str, team_id: str, cache: Cache | None = None, timeout: float = 8.0):
        self._headers = {"X-Api-Key": api_key, "X-Team-ID": team_id}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._cache = cache
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session:
            await self._session.close()

    async def _get(self, path: str, **params) -> dict:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if self._cache and (cached := self._cache.get(key)) is not None:
            return cached
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        try:
            async with self._session.get(API + path, params=params) as response:
                if response.status in (401, 403):
                    raise ScrydexError("Scrydex refused the API key or team ID. Run `pokescan login` again.")
                if response.status == 429:
                    raise ScrydexError("Scrydex rate limit hit; try again in a moment.")
                if response.status >= 400:
                    raise ScrydexError(f"Scrydex error {response.status}: {(await response.text())[:200]}")
                body = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ScrydexError(f"couldn't reach Scrydex ({e or type(e).__name__})") from e
        if self._cache:
            self._cache.put(key, body)
        return body

    async def search(self, query: str, page_size: int = 25) -> list[dict]:
        """Cards matching a Scrydex query, with prices. 1 credit, then cached."""
        return (await self._get("/cards", q=query, include="prices", page_size=str(page_size))).get("data", [])

    async def card(self, card_id: str) -> dict:
        return (await self._get(f"/cards/{card_id}", include="prices")).get("data", {})

    async def expansion_cards(self, expansion_id: str) -> list[dict]:
        """Every card in a set, with prices: a few credits once a day, then free to match against."""
        cards, page = [], 1
        while True:
            body = await self._get(f"/expansions/{expansion_id}/cards", include="prices", page_size="100", page=str(page))
            cards += body.get("data", [])
            if len(cards) >= body.get("total_count", 0) or not body.get("data"):
                return cards
            page += 1

    async def expansions(self, name: str) -> list[dict]:
        query = " ".join(f"name:{w}*" for w in re.findall(r"[A-Za-z0-9]+", name)) or "*"
        body = await self._get("/expansions", q=query, page_size="10", orderBy="-release_date")
        return [{"id": e["id"], "name": e["name"], "series": e.get("series"), "total": e.get("printed_total"),
                 "release_date": e.get("release_date")} for e in body.get("data", [])]
