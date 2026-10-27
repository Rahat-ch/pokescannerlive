import asyncio

import pytest
from aiohttp import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from pokescan.server import load_token, make_app
from pokescan.session import SessionStore

TOKEN = "test-token"
AUTH = {"X-Pokescan-Token": TOKEN}


@pytest.fixture
async def client(tmp_path):
    store = SessionStore(tmp_path / "pokescan.db")
    client = TestClient(TestServer(make_app(store, TOKEN)))
    await client.start_server()
    yield client
    await client.close()
    store.close()


def own_origin(client) -> dict:
    return {"Origin": f"http://{client.host}:{client.port}"}


async def test_pages_need_the_token(client):
    assert (await client.get("/overlay")).status == 403
    assert (await client.get("/overlay?token=wrong")).status == 403
    resp = await client.get(f"/overlay?token={TOKEN}")
    assert resp.status == 200 and "Session total" in await resp.text()
    assert (await client.get(f"/control?token={TOKEN}")).status == 200


async def test_add_undo_and_state(client):
    resp = await client.post("/scans", json={"name": "Charizard ex", "price": "120", "variant": "Holo"},
                             headers={**AUTH, **own_origin(client)})
    assert resp.status == 201
    scan = await resp.json()
    assert scan["price"] == "120.00" and scan["copy"] == 1

    state = await (await client.get("/state", headers=AUTH)).json()
    assert state["count"] == 1 and state["totals"] == [{"currency": "USD", "amount": "120.00"}]

    assert (await client.post("/undo", headers=AUTH)).status == 200
    assert (await client.post("/undo", headers=AUTH)).status == 404
    assert (await client.post(f"/scans/{scan['id']}/restore", headers=AUTH)).status == 200
    assert (await (await client.get("/state", headers=AUTH)).json())["count"] == 1


async def test_bad_scan_is_a_400_not_a_crash(client):
    for body in [{"name": "", "price": "1"}, {"name": "A", "price": "lots"}, ["not", "an", "object"]]:
        assert (await client.post("/scans", json=body, headers=AUTH)).status == 400


async def test_cross_origin_writes_and_sockets_are_refused(client):
    evil = {**AUTH, "Origin": "https://evil.example"}
    assert (await client.post("/scans", json={"name": "A", "price": "1"}, headers=evil)).status == 403
    with pytest.raises(WSServerHandshakeError):
        await client.ws_connect(f"/ws?token={TOKEN}", headers={"Origin": "https://evil.example"})
    assert (await (await client.get("/state", headers=AUTH)).json())["count"] == 0


async def test_websocket_pushes_state_on_every_change(client):
    ws = await client.ws_connect(f"/ws?token={TOKEN}", headers=own_origin(client))
    assert (await ws.receive_json())["count"] == 0
    await client.post("/scans", json={"name": "Pikachu", "price": "0.35"}, headers=AUTH)
    pushed = await ws.receive_json(timeout=2)
    assert pushed["last"]["name"] == "Pikachu" and pushed["count"] == 1
    await client.post("/undo", headers=AUTH)
    assert (await ws.receive_json(timeout=2))["last"] is None
    await ws.close()


async def test_new_session_and_csv_export(client):
    await client.post("/scans", json={"name": "Mew", "price": "4"}, headers=AUTH)
    resp = await client.get(f"/export.csv?token={TOKEN}")
    assert resp.status == 200 and "attachment" in resp.headers["Content-Disposition"]
    assert "Mew" in await resp.text()
    state = await (await client.post("/session/new", headers=AUTH)).json()
    assert state["count"] == 0


def test_token_is_created_once_and_kept(tmp_path):
    token = load_token(tmp_path)
    assert len(token) >= 16
    assert load_token(tmp_path) == token
    assert (tmp_path / "token").stat().st_mode & 0o077 == 0


class FakeScanner:
    def __init__(self):
        self.taken, self.dismissed = [], []
        self.on_change = lambda: None

    def snapshot(self):
        return {"source": "Camera", "connected": True, "problem": None, "status": "no card", "captures": []}

    def take(self, crop):
        self.taken.append(crop)

    def dismiss(self, capture_id):
        self.dismissed.append(capture_id)
        return capture_id == 1

    async def run(self):
        await asyncio.Event().wait()


@pytest.fixture
async def scanning_client(tmp_path):
    store = SessionStore(tmp_path / "pokescan.db")
    crops = tmp_path / "crops"
    crops.mkdir()
    (crops / "20261027-190000-0001.jpg").write_bytes(b"\xff\xd8 jpeg")
    (tmp_path / "secret.txt").write_text("not for you")
    scanner = FakeScanner()
    client = TestClient(TestServer(make_app(store, TOKEN, scanner, crops)))
    await client.start_server()
    yield client, scanner
    await client.close()
    store.close()


async def test_state_includes_scanner_only_when_scanning(client, scanning_client):
    assert (await (await client.get("/state", headers=AUTH)).json())["scanner"] is None
    sc, _ = scanning_client
    assert (await (await sc.get("/state", headers=AUTH)).json())["scanner"]["source"] == "Camera"


async def test_crops_are_served_by_exact_name_only(scanning_client):
    sc, _ = scanning_client
    ok = await sc.get(f"/crops/20261027-190000-0001.jpg?token={TOKEN}")
    assert ok.status == 200 and await ok.read() == b"\xff\xd8 jpeg"
    assert (await sc.get("/crops/20261027-190000-0001.jpg")).status == 403
    for name in ["..%2Fsecret.txt", "secret.txt", "20261027-190000-0002.jpg"]:
        assert (await sc.get(f"/crops/{name}?token={TOKEN}")).status == 404


async def test_adding_a_scanned_card_attaches_its_crop(scanning_client):
    sc, scanner = scanning_client
    resp = await sc.post("/scans", json={"name": "Glaceon", "price": "0.50", "crop": "20261027-190000-0001.jpg"},
                         headers=AUTH)
    assert resp.status == 201 and (await resp.json())["crop"] == "20261027-190000-0001.jpg"
    assert scanner.taken == ["20261027-190000-0001.jpg"]
    bad = await sc.post("/scans", json={"name": "Glaceon", "price": "1", "crop": "../../etc/passwd"}, headers=AUTH)
    assert bad.status == 400


async def test_dismiss_capture(scanning_client, client):
    sc, scanner = scanning_client
    assert (await sc.post("/captures/1/dismiss", headers=AUTH)).status == 200
    assert (await sc.post("/captures/9/dismiss", headers=AUTH)).status == 404
    assert (await client.post("/captures/1/dismiss", headers=AUTH)).status == 404  # scanning off


async def test_packs_being_opened_are_kept_and_an_old_single_set_still_counts(tmp_path):
    import json

    from pokescan.server import Settings

    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"stream_set": {"id": "sv8pt5", "name": "Prismatic Evolutions"}}))
    store = SessionStore(tmp_path / "pokescan.db")
    client = TestClient(TestServer(make_app(store, TOKEN, settings=Settings(path))))
    await client.start_server()
    try:
        state = await (await client.get("/state", headers=AUTH)).json()
        assert [x["id"] for x in state["stream_sets"]] == ["sv8pt5"]
        sets = [{"id": "sv8pt5", "name": "Prismatic Evolutions"}, {"id": "me5", "name": "Pitch Black"},
                {"id": "me5", "name": "Pitch Black"}]
        resp = await client.post("/settings/sets", json={"sets": sets}, headers=AUTH)
        assert [x["id"] for x in await resp.json()] == ["sv8pt5", "me5"]  # duplicates dropped
        assert [x["id"] for x in Settings(path).stream_sets()] == ["sv8pt5", "me5"]  # outlives a restart
        assert (await client.post("/settings/sets", json={"sets": "me5"}, headers=AUTH)).status == 400
        assert (await client.post("/settings/sets", json={"sets": []}, headers=AUTH)).status == 200
        assert Settings(path).stream_sets() == []
    finally:
        await client.close()
        store.close()
