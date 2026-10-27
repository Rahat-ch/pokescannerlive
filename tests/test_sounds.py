import urllib.error

from aiohttp.test_utils import TestClient, TestServer

from pokescan.scrydex import sound
from pokescan.server import make_app
from pokescan.session import SessionStore
from pokescan.sounds import Cries, custom

TOKEN = "test-token"
AUTH = {"X-Pokescan-Token": TOKEN}


def test_each_kind_of_card_has_its_sound():
    assert sound({"supertype": "Pokémon", "subtypes": ["Basic"], "national_pokedex_numbers": [151]}) == "cry:151"
    assert sound({"supertype": "Pokémon", "national_pokedex_numbers": [6, 150]}) == "cry:6"  # the first Pokémon
    assert sound({"supertype": "Trainer", "subtypes": ["Supporter"]}) == "supporter"
    assert sound({"supertype": "Trainer", "subtypes": ["Pokémon Tool", "ACE SPEC"]}) == "item"
    assert sound({"supertype": "Trainer", "subtypes": ["Item", "ACE SPEC"]}) == "item"
    assert sound({"supertype": "Trainer", "subtypes": ["Stadium"]}) == "stadium"
    assert sound({"supertype": "Energy", "subtypes": ["Basic"]}) == "energy"
    assert sound({}) is None  # unknown: the overlay plays the cash ping


async def test_cries_prefer_the_original_and_download_once(tmp_path):
    asked = []

    def fetch(url):
        asked.append(url)
        if "/legacy/" in url and "/25." in url:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return b"OggS" + url.encode()

    cries = Cries(tmp_path / "cries", fetch)
    assert (await cries.path(151)).read_bytes().endswith(b"/legacy/151.ogg")
    assert (await cries.path(151)).name == "151.ogg" and len(asked) == 1  # kept on disk
    assert (await cries.path(25)).read_bytes().endswith(b"/latest/25.ogg")  # no original: the current one
    assert (await cries.path(906)).read_bytes().endswith(b"/latest/906.ogg")  # Gen 9 has no original
    assert await cries.path(0) is None and await cries.path(99999) is None


def test_own_sounds_are_found_by_name_only(tmp_path):
    (tmp_path / "supporter.mp3").write_bytes(b"x")
    (tmp_path / "secret.txt").write_bytes(b"x")
    assert custom(tmp_path) == {"supporter": "supporter.mp3"}
    assert custom(None) == {}


async def test_sound_routes(tmp_path):
    sounds = tmp_path / "sounds"
    sounds.mkdir()
    (sounds / "energy.ogg").write_bytes(b"zap")
    (sounds / "notes.txt").write_bytes(b"private")
    store = SessionStore(tmp_path / "s.db")
    store.add("Mew ex", "120", sound="cry:151")
    cries = Cries(tmp_path / "cries", lambda url: b"OggS")
    client = TestClient(TestServer(make_app(store, TOKEN, cries=cries, sounds_dir=sounds)))
    await client.start_server()
    try:
        state = await (await client.get("/state", headers=AUTH)).json()
        assert state["last"]["sound"] == "cry:151" and state["sounds"] == {"energy": "energy.ogg"}
        resp = await client.get(f"/cries/151?token={TOKEN}")
        assert resp.status == 200 and resp.headers["Content-Type"] == "audio/ogg"
        assert (await client.get("/cries/151")).status == 403  # the token guards it like everything else
        assert (await client.get(f"/sounds/energy.ogg?token={TOKEN}")).status == 200
        assert (await client.get(f"/sounds/notes.txt?token={TOKEN}")).status == 404
        assert (await client.get(f"/sounds/..%2Fs.db?token={TOKEN}")).status == 404
    finally:
        await client.close()
        store.close()


def test_old_databases_gain_the_sound_column(tmp_path):
    import sqlite3

    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""
        CREATE TABLE sessions (id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT);
        CREATE TABLE scans (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, added_at TEXT NOT NULL,
            name TEXT NOT NULL, set_name TEXT, number TEXT, variant TEXT, price_minor INTEGER NOT NULL,
            currency TEXT NOT NULL, source TEXT NOT NULL, card_id TEXT, voided_at TEXT);
        INSERT INTO sessions (started_at) VALUES ('2026-10-27T18:00:00+00:00');
        INSERT INTO scans VALUES (1, 1, 'x', 'Pinsir', NULL, NULL, NULL, 50, 'USD', 'manual', NULL, NULL);
    """)
    db.commit()
    db.close()
    store = SessionStore(tmp_path / "old.db")
    assert store.get(1).sound is None and store.add("Mew", "1", sound="cry:151").sound == "cry:151"
    store.close()
