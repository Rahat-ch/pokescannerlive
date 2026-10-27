"""The local web server behind the OBS overlay and the control dock.

Bound to 127.0.0.1. Every request needs the install's token (OBS Browser Source
and dock URLs carry it), and state-changing requests and WebSockets must come
from our own origin, so another web page in the same browser can't drive it.
"""

import io
import json
import secrets
import weakref
from importlib.resources import files
from pathlib import Path

from aiohttp import WSMsgType, web

from pokescan.session import SessionStore

STORE = web.AppKey("store", SessionStore)
TOKEN = web.AppKey("token", str)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)

PAGES = files("pokescan") / "web"


def load_token(data_dir: Path) -> str:
    """A token kept across runs, so the URLs pasted into OBS keep working."""
    path = data_dir / "token"
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(16)
    path.write_text(token)
    path.chmod(0o600)
    return token


@web.middleware
async def guard(request: web.Request, handler):
    token = request.query.get("token") or request.headers.get("X-Pokescan-Token", "")
    if not secrets.compare_digest(token, request.app[TOKEN]):
        raise web.HTTPForbidden(text="missing or wrong token")
    origin = request.headers.get("Origin")
    is_ws = request.headers.get("Upgrade", "").lower() == "websocket"
    if (request.method != "GET" or is_ws) and origin is not None and origin != f"http://{request.host}":
        raise web.HTTPForbidden(text="cross-origin request refused")
    return await handler(request)


async def broadcast(app: web.Application) -> None:
    message = json.dumps(app[STORE].state())
    for ws in list(app[SOCKETS]):
        if not ws.closed:
            await ws.send_str(message)


def page(name: str):
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=(PAGES / name).read_text(), content_type="text/html")
    return handler


async def index(request: web.Request) -> web.Response:
    raise web.HTTPFound(f"/control?token={request.app[TOKEN]}")


async def state(request: web.Request) -> web.Response:
    return web.json_response(request.app[STORE].state())


async def socket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    await ws.send_str(json.dumps(request.app[STORE].state()))
    async for msg in ws:  # Clients only listen; drain until they go away.
        if msg.type == WSMsgType.ERROR:
            break
    return ws


async def add_scan(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        scan = request.app[STORE].add(
            name=str(body.get("name", "")),
            price=str(body.get("price", "")),
            currency=str(body.get("currency") or "USD"),
            set_name=body.get("set_name"),
            number=body.get("number"),
            variant=body.get("variant"),
        )
    except (ValueError, AttributeError) as e:
        raise web.HTTPBadRequest(text=str(e) or "expected a JSON object")
    await broadcast(request.app)
    return web.json_response(scan.to_json(), status=201)


def scan_action(method: str):
    async def handler(request: web.Request) -> web.Response:
        store = request.app[STORE]
        if method == "undo":
            scan = store.undo()
        else:
            scan = getattr(store, method)(int(request.match_info["scan_id"]))
        if scan is None:
            raise web.HTTPNotFound(text="no such scan")
        await broadcast(request.app)
        return web.json_response(scan.to_json())
    return handler


async def new_session(request: web.Request) -> web.Response:
    request.app[STORE].new_session()
    await broadcast(request.app)
    return web.json_response(request.app[STORE].state())


async def export_csv(request: web.Request) -> web.Response:
    store = request.app[STORE]
    out = io.StringIO()
    store.export_csv(out)
    filename = f"pokescan-session-{store.session_id}-{store.started_at()[:10]}.csv"
    return web.Response(
        text=out.getvalue(), content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def make_app(store: SessionStore, token: str) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[STORE] = store
    app[TOKEN] = token
    app[SOCKETS] = weakref.WeakSet()
    app.router.add_get("/", index)
    app.router.add_get("/overlay", page("overlay.html"))
    app.router.add_get("/control", page("control.html"))
    app.router.add_get("/state", state)
    app.router.add_get("/ws", socket)
    app.router.add_post("/scans", add_scan)
    app.router.add_post("/undo", scan_action("undo"))
    app.router.add_post(r"/scans/{scan_id:\d+}/void", scan_action("void"))
    app.router.add_post(r"/scans/{scan_id:\d+}/restore", scan_action("restore"))
    app.router.add_post("/session/new", new_session)
    app.router.add_get("/export.csv", export_csv)
    return app
