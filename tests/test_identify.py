from decimal import Decimal

import cv2
import numpy as np
import pytest

from pokescan import credentials
from pokescan.identify import (Identifier, digits_off, is_confident, name_queries, name_similarity, rank)
from pokescan.ocr import CardText, Line, parse
from pokescan.scanner import Capture
from pokescan.scrydex import ScrydexError, near_mint, summary, variant_label
from pokescan.session import SessionStore


def card(card_id, name, set_id="s1", set_name="Set One", number="62", total=84, hp="160", variants=None, lang="EN"):
    """A hand-made card in Scrydex's response shape."""
    variants = variants if variants is not None else {"normal": 1.23, "holofoil": 0.24, "reverseHolofoil": 1.06}
    return {
        "id": card_id, "name": name, "hp": hp, "number": number, "printed_number": f"{int(number):03d}/{total:03d}",
        "language_code": lang,
        "expansion": {"id": set_id, "name": set_name, "printed_total": total},
        "images": [{"type": "front", "small": f"https://img/{card_id}/small", "medium": f"https://img/{card_id}/medium"}],
        "variants": [{"name": v, "prices": [{"type": "raw", "condition": "LP", "market": p / 2, "currency": "USD"},
                                            {"type": "raw", "condition": "NM", "market": p, "currency": "USD"}]}
                     for v, p in variants.items()],
    }


# --- scrydex helpers -------------------------------------------------------------------------

def test_variant_labels():
    assert variant_label("normal") == "Normal"
    assert variant_label("holofoil") == "Holo"
    assert variant_label("unlimitedHolofoil") == "Holo"
    assert variant_label("reverseHolofoil") == "Reverse Holo"
    assert variant_label("firstEditionShadowlessHolofoil") == "1st Edition Shadowless Holo"


def test_near_mint_and_summary():
    c = card("me5-62", "Bastiodon")
    assert near_mint(c["variants"][0]).amount == Decimal("1.23")
    s = summary(c)
    assert s["number"] == "062/084" and s["image"] == "https://img/me5-62/small"
    assert [(v["label"], v["price"]) for v in s["variants"]] == [("Normal", "1.23"), ("Holo", "0.24"), ("Reverse Holo", "1.06")]
    assert near_mint({"name": "x", "prices": [{"type": "graded", "market": 9}]}) is None


# --- reading the card ------------------------------------------------------------------------

def test_parse_picks_name_hp_and_number_by_position():
    lines = [Line("BASIC", 0.02, 0.01, 0.023, 1), Line("Bastiodon", 0.04, 0.18, 0.044, 1),
             Line("-160", 0.04, 0.71, 0.054, 1), Line("Ancient Bulwark", 0.5, 0.2, 0.03, 1),
             Line("O CEO 062/084 *", 0.95, 0.05, 0.031, 1)]
    text = parse(lines)
    assert (text.name, text.hp, text.number, text.total) == ("Bastiodon", 160, "062", "084")


def test_parse_trainer_has_no_hp():
    lines = [Line("Supporter", 0.01, 0.04, 0.027, 1), Line("TRAINER", 0.01, 0.59, 0.032, 1),
             Line("Black Belt's Training", 0.06, 0.04, 0.055, 1)]
    text = parse(lines)
    assert (text.name, text.hp) == ("Black Belt's Training", None)


def test_parse_fixes_common_digit_misreads():
    assert parse([Line("Ol0/O84", 0.95, 0.1, 0.03, 1)]).number == "010"


# --- matching --------------------------------------------------------------------------------

def test_name_similarity_survives_garbled_leading_words():
    assert name_similarity("Bastiodon", "Bastiodon") == 1.0
    assert name_similarity("merton tal Ogerpon eX", "Cornerstone Mask Ogerpon ex") > 0.25
    assert name_similarity("Pinsir", "Mankey") < 0.4


def test_digits_off():
    assert digits_off("042", "62") == 1
    assert digits_off("064", 84) == 1
    assert digits_off("070", "70") == 0
    assert digits_off(None, "70") is None


def test_hp_and_number_pick_the_printing():
    text = CardText("Bastiodon", 160, "042", "064")  # a real misread of 062/084
    ranked = rank([card("a", "Bastiodon", number="62"), card("b", "Bastiodon", number="93"),
                   card("c", "Bastiodon", set_id="s2", number="110", total=189, hp="140")], text, None)
    assert [c["id"] for _, c in ranked] == ["a", "b", "c"]


def test_stream_set_breaks_ties():
    text = CardText("Mankey", 50, None, None)
    cards = [card("old", "Mankey", set_id="old", hp="50"), card("new", "Mankey", set_id="me5", hp="50")]
    assert not is_confident(rank(cards, text, None))
    ranked = rank(cards, text, ["me5"])
    assert ranked[0][1]["id"] == "new" and is_confident(ranked)


def test_name_queries_narrow_by_hp_and_physical_english():
    queries = name_queries("merton tal Ogerpon eX", 210)
    assert queries[0] == 'name:"merton tal Ogerpon eX" hp:210 language_code:EN -expansion.is_online_only:true'
    assert any('name:"Ogerpon"' in q for q in queries)


# --- the identifier end to end ---------------------------------------------------------------

class FakeScrydex:
    def __init__(self, cards, fail=False):
        self.cards, self.fail, self.queries = cards, fail, []

    async def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise ScrydexError("couldn't reach Scrydex (timeout)")
        word = query.split('"')[1].lower()
        return [c for c in self.cards if word in c["name"].lower()]

    async def expansion_cards(self, set_id):
        return [c for c in self.cards if c["expansion"]["id"] == set_id]

    async def card(self, card_id):
        return next(c for c in self.cards if c["id"] == card_id)

    async def close(self):
        pass


class FakeScanner:
    def __init__(self, capture):
        self.captures = [capture]
        self.taken, self.dismissed = [], []
        self.on_capture = None

    def update(self, capture, **fields):
        for k, v in fields.items():
            setattr(capture, k, v)

    def get(self, capture_id):
        return next((c for c in self.captures if c.id == capture_id), None)

    def take(self, crop):
        self.taken.append(crop)

    def dismiss(self, capture_id):
        self.dismissed.append(capture_id)


@pytest.fixture
def env(tmp_path):
    crops = tmp_path / "crops"
    crops.mkdir()
    cv2.imwrite(str(crops / "20261027-190000-0001.jpg"), np.full((880, 630, 3), 128, np.uint8))
    store = SessionStore(tmp_path / "s.db")
    yield tmp_path, crops, store
    store.close()


def make(env, cards, text, capture=None, stream_set=None, fail=False):
    tmp_path, crops, store = env
    capture = capture or Capture(1, "2026-10-27T19:00:00+00:00", "20261027-190000-0001.jpg", 0.96, 0.75,
                                 presentation=1, state="identifying")
    scanner = FakeScanner(capture)
    ident = Identifier(FakeScrydex(cards, fail), store, scanner, crops, lambda: [stream_set] if stream_set else [],
                       read_card=lambda path: text)
    return ident, scanner, capture, store


async def test_one_priced_variant_is_added_at_once(env):
    cards = [card("pe-3", "Pinsir", hp="120", number="3", total=131, variants={"normal": 0.5})]
    ident, scanner, capture, store = make(env, cards, CardText("Pinsir", 120, None, None))
    await ident.identify(capture)
    scan = store.state()["last"]
    assert (scan["name"], scan["price"], scan["variant"], scan["card_id"], scan["source"]) == \
        ("Pinsir", "0.50", "Normal", "pe-3", "scanner")
    assert scan["crop"] == capture.crop and scanner.taken == [capture.crop]


async def test_several_variants_add_the_standard_printing_then_switch(env):
    ident, scanner, capture, store = make(env, [card("me5-62", "Bastiodon")], CardText("Bastiodon", 160, None, None))
    await ident.identify(capture)
    last = store.state()["last"]
    assert (last["variant"], last["price"]) == ("Normal", "1.23")  # priced on stream at once
    assert [v["label"] for v in ident.variants_for("me5-62")] == ["Normal", "Holo", "Reverse Holo"]
    scan = await ident.change_variant(last["id"], "reverseHolofoil")
    assert (scan.variant, scan.price) == ("Reverse Holo", Decimal("1.06"))
    assert store.totals() == {"USD": Decimal("1.06")}


async def test_holo_only_card_defaults_to_holo(env):
    cards = [card("r-1", "Charizard", hp="170", variants={"reverseHolofoil": 3.0, "holofoil": 9.5})]
    ident, scanner, capture, store = make(env, cards, CardText("Charizard", 170, None, None))
    await ident.identify(capture)
    assert store.state()["last"]["variant"] == "Holo"


async def test_lookalike_reprints_add_a_best_guess_that_can_be_swapped(env):
    # Same name, same art, a number too small to read: price the best guess now, swap it in one click.
    cards = [card("a", "Black Belt's Training", set_id="a", set_name="Set A", hp=None, number="145", total=159,
                  variants={"normal": 0.18, "reverseHolofoil": 0.34}),
             card("b", "Black Belt's Training", set_id="b", set_name="Set B", hp=None, number="98", total=131,
                  variants={"normal": 0.08, "reverseHolofoil": 0.13})]
    ident, scanner, capture, store = make(env, cards, CardText("Black Belt's Training", None, None, None))
    await ident.identify(capture)
    last = store.state()["last"]
    assert last["price"] == "0.18" and scanner.taken == [capture.crop]
    assert {c["card_id"] for c in ident.alternatives_for(last["id"])} == {"a", "b"}
    await ident.change_variant(last["id"], "reverseHolofoil")
    scan = await ident.change_card(last["id"], "b")
    assert (scan.card_id, scan.set_name, scan.number, scan.variant, scan.price) == \
        ("b", "Set B", "098/131", "Reverse Holo", Decimal("0.13"))  # the printing carries over
    assert store.totals() == {"USD": Decimal("0.13")} and store.state()["count"] == 1
    with pytest.raises(LookupError):
        await ident.change_card(last["id"], "not-a-lookalike")


async def test_packs_being_opened_beat_reprints_elsewhere(env):
    cards = [card("a", "Black Belt's Training", set_id="sv9", hp=None), card("b", "Black Belt's Training", set_id="sv8pt5", hp=None)]
    ident, scanner, capture, store = make(env, cards, CardText("Black Belt's Training", None, None, None),
                                          stream_set="sv8pt5")
    await ident.identify(capture)
    assert store.state()["last"]["card_id"] == "b" and ident.scrydex.queries == []
    assert ident.alternatives_for(store.state()["last"]["id"]) is None  # a clear match, not a guess


async def test_unsure_name_offers_candidates(env):
    cards = [card("a", "Cornerstone Mask Ogerpon ex", hp="210"), card("b", "Wellspring Mask Ogerpon ex", hp="210")]
    ident, scanner, capture, store = make(env, cards, CardText("merton tal Ogerpon eX", 210, None, None))
    await ident.identify(capture)
    assert capture.state == "pick card" and {c["card_id"] for c in capture.candidates} == {"a", "b"}
    assert store.state()["count"] == 0


async def test_unreadable_or_unreachable_falls_back_to_typing(env):
    ident, scanner, capture, store = make(env, [], CardText(None, None, None, None))
    await ident.identify(capture)
    assert capture.state == "unknown" and "Type" in capture.message
    ident, scanner, capture, store = make(env, [], CardText("Pinsir", 120, None, None), fail=True)
    await ident.identify(capture)
    assert capture.state == "unknown" and "couldn't reach Scrydex" in capture.message


async def test_same_card_rescanned_without_lowering_is_dropped(env):
    cards = [card("pe-3", "Pinsir", hp="120", variants={"normal": 0.5})]
    ident, scanner, capture, store = make(env, cards, CardText("Pinsir", 120, None, None))
    await ident.identify(capture)
    again = Capture(2, capture.at, capture.crop, 0.96, 0.75, presentation=1, state="identifying")
    scanner.captures.append(again)
    await ident.identify(again)
    assert scanner.dismissed == [2] and store.state()["count"] == 1
    shown_again = Capture(3, capture.at, capture.crop, 0.96, 0.75, presentation=2, state="identifying")
    await ident.identify(shown_again)
    assert store.state()["count"] == 2  # lowered and shown again: a repeat, which counts


async def test_stream_set_is_matched_without_a_search(env):
    cards = [card("me5-42", "Mankey", set_id="me5", hp="50", variants={"normal": 0.2})]
    ident, scanner, capture, store = make(env, cards, CardText("Mankey", 50, None, None), stream_set="me5")
    await ident.identify(capture)
    assert ident.scrydex.queries == [] and store.state()["last"]["card_id"] == "me5-42"


# --- credentials -----------------------------------------------------------------------------

def test_credentials_are_private_and_env_wins(tmp_path):
    path = credentials.save_scrydex(tmp_path, " key123 ", "team")
    assert path.stat().st_mode & 0o077 == 0
    assert credentials.scrydex(tmp_path, env={}) == ("key123", "team")
    assert credentials.scrydex(tmp_path, env={"SCRYDEX_API_KEY": "k2", "SCRYDEX_TEAM_ID": "t2"}) == ("k2", "t2")
    assert credentials.scrydex(tmp_path / "none", env={}) is None


# --- the printing check ----------------------------------------------------------------------

class FakeJudge:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    async def judge(self, jpeg, name, variants):
        self.asked.append((name, variants))
        return self.answer

    async def close(self):
        pass


async def test_judged_printing_is_used(env):
    ident, scanner, capture, store = make(env, [card("me5-62", "Bastiodon")], CardText("Bastiodon", 160, None, None))
    ident.judge = FakeJudge("reverseHolofoil")
    await ident.identify(capture)
    assert ident.judge.asked == [("Bastiodon", ["normal", "holofoil", "reverseHolofoil"])]
    assert (store.state()["last"]["variant"], store.state()["last"]["price"]) == ("Reverse Holo", "1.06")


async def test_unsure_judge_falls_back_to_the_standard_printing(env):
    ident, scanner, capture, store = make(env, [card("me5-62", "Bastiodon")], CardText("Bastiodon", 160, None, None))
    ident.judge = FakeJudge(None)
    await ident.identify(capture)
    assert store.state()["last"]["variant"] == "Normal"


async def test_single_printing_skips_the_judge(env):
    cards = [card("pe-3", "Pinsir", hp="120", variants={"normal": 0.5})]
    ident, scanner, capture, store = make(env, cards, CardText("Pinsir", 120, None, None))
    ident.judge = FakeJudge("holofoil")
    await ident.identify(capture)
    assert ident.judge.asked == [] and store.state()["last"]["variant"] == "Normal"


def test_judge_request_only_allows_the_cards_variants():
    from pokescan.variants import MODEL, request_body
    body = request_body(b"\xff\xd8jpeg", "Bastiodon", ["normal", "reverseHolofoil"])
    assert body["model"] == MODEL == "anthropic/claude-haiku-5.5"
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["variant"]["enum"] == ["normal", "reverseHolofoil", "unsure"]
    assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "Reverse Holo (the card body" in body["messages"][0]["content"][1]["text"]
