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
