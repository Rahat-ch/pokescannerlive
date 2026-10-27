import csv
import io
from decimal import Decimal

import pytest

from pokescan.session import SessionStore, from_minor, to_minor


class Ticker:
    def __init__(self):
        self.n = 0

    def __call__(self) -> str:
        self.n += 1
        return f"2026-10-27T20:{self.n // 60:02d}:{self.n % 60:02d}+00:00"


@pytest.fixture
def store(tmp_path):
    s = SessionStore(tmp_path / "pokescan.db", clock=Ticker())
    yield s
    s.close()


def test_price_conversion():
    assert to_minor("12.50", "USD") == 1250
    assert to_minor("$0.25", "USD") == 25
    assert to_minor("1500", "JPY") == 1500
    assert from_minor(1250, "USD") == Decimal("12.50")
    for bad in ["", "abc", "-1", "1.005", "nan", "inf"]:
        with pytest.raises(ValueError):
            to_minor(bad, "USD")
    with pytest.raises(ValueError):
        to_minor("1500.5", "JPY")


def test_adds_cards_and_totals_them(store):
    store.add("Charizard ex", "120.00", set_name="Obsidian Flames", number="125/197", variant="Holo")
    store.add("Pikachu", "0.35")
    assert store.totals() == {"USD": Decimal("120.35")}
    state = store.state()
    assert state["count"] == 2
    assert state["last"]["name"] == "Pikachu"
    assert state["totals"] == [{"currency": "USD", "amount": "120.35"}]


def test_repeats_count_and_are_numbered(store):
    store.add("Pikachu", "0.35", set_name="151", variant="Normal")
    store.add("Pikachu", "0.35", set_name="151", variant="Reverse Holo")  # a different printing
    third = store.add("pikachu", "0.35", set_name="151", variant="normal")
    assert third.copy == 2
    assert store.totals() == {"USD": Decimal("1.05")}
    assert [s.copy for s in store.scans()] == [2, 1, 1]


def test_card_id_decides_identity_when_present(store):
    store.add("Mew ex", "5", card_id="sv3pt5-151")
    again = store.add("Mew ex (alt name)", "5", card_id="sv3pt5-151")
    assert again.copy == 2


def test_undo_voids_the_latest_and_restore_brings_it_back(store):
    store.add("Bulbasaur", "1")
    mew = store.add("Mew", "4")
    undone = store.undo()
    assert undone.id == mew.id and undone.voided_at is not None
    assert store.totals() == {"USD": Decimal("1.00")}
    assert store.state()["last"]["name"] == "Bulbasaur"
    store.restore(mew.id)
    assert store.totals() == {"USD": Decimal("5.00")}


def test_undo_skips_already_voided_and_stops_when_empty(store):
    a = store.add("A", "1")
    b = store.add("B", "2")
    store.void(b.id)
    assert store.undo().id == a.id
    assert store.undo() is None
    assert store.totals() == {}


def test_voided_repeat_does_not_take_a_copy_number(store):
    first = store.add("Eevee", "1")
    store.void(first.id)
    assert store.add("Eevee", "1").copy == 1


def test_currencies_are_totalled_separately(store):
    store.add("A", "10", currency="USD")
    store.add("B", "1500", currency="jpy")
    assert store.totals() == {"USD": Decimal("10.00"), "JPY": Decimal("1500")}


def test_rejects_bad_input(store):
    with pytest.raises(ValueError):
        store.add("  ", "1")
    with pytest.raises(ValueError):
        store.add("A", "1", currency="dollars")
    assert store.scans() == []


def test_session_survives_restart_until_a_new_one_starts(tmp_path):
    path = tmp_path / "pokescan.db"
    first = SessionStore(path)
    first.add("Charizard", "100")
    session_id = first.session_id
    first.close()

    resumed = SessionStore(path)
    assert resumed.session_id == session_id
    assert resumed.totals() == {"USD": Decimal("100.00")}
    new_id = resumed.new_session()
    assert new_id != session_id
    assert resumed.scans() == [] and resumed.totals() == {}
    resumed.close()

    assert SessionStore(path).session_id == new_id


def test_void_only_touches_the_current_session(store):
    old = store.add("Old", "1")
    store.new_session()
    assert store.void(old.id) is None


def test_csv_export_lists_every_scan_oldest_first(store):
    store.add("Pikachu", "0.35", set_name="151", number="025/165", variant="Normal")
    store.add("Pikachu", "0.35", set_name="151", number="025/165", variant="Normal")
    store.add("Mew", "4.00")
    store.undo()
    out = io.StringIO()
    store.export_csv(out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [(r["name"], r["copy"], r["status"]) for r in rows] == [
        ("Pikachu", "1", "counted"), ("Pikachu", "2", "counted"), ("Mew", "", "voided"),
    ]
    assert rows[0]["price"] == "0.35" and rows[0]["number"] == "025/165"


def test_scans_keep_their_crop_and_old_databases_are_migrated(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT);
        CREATE TABLE scans (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, added_at TEXT NOT NULL,
            name TEXT NOT NULL, set_name TEXT, number TEXT, variant TEXT, price_minor INTEGER NOT NULL,
            currency TEXT NOT NULL, source TEXT NOT NULL, card_id TEXT, voided_at TEXT);
        INSERT INTO sessions (started_at) VALUES ('2026-10-27T18:00:00+00:00');
        INSERT INTO scans (session_id, added_at, name, price_minor, currency, source)
            VALUES (1, '2026-10-27T18:01:00+00:00', 'Old card', 100, 'USD', 'manual');
    """)
    old.commit()
    old.close()
    store = SessionStore(path)
    assert store.scans()[0].crop is None
    scan = store.add("Glaceon", "0.50", crop="20261027-190000-0001.jpg")
    assert scan.crop == "20261027-190000-0001.jpg"
    assert store.state()["last"]["crop"] == "20261027-190000-0001.jpg"
    store.close()
