"""The local web server behind the OBS overlay and the control dock.

Bound to 127.0.0.1. Every request needs the install's token (OBS Browser Source
and dock URLs carry it), and state-changing requests and WebSockets must come
from our own origin, so another web page in the same browser can't drive it.
"""

import asyncio
import contextlib
import io
import json
import re
import secrets
import weakref
from importlib.resources import files
from pathlib import Path

from aiohttp import WSMsgType, web

from pokescan.session import SessionStore

STORE = web.AppKey("store", SessionStore)
TOKEN = web.AppKey("token", str)
SOCKETS = web.AppKey("sockets", weakref.WeakSet)
SCANNER = web.AppKey("scanner", object)  # pokescan.scanner.Scanner, or None when scanning is off
CROPS = web.AppKey("crops", object)  # Path of the crops directory, or None

IDENTIFIER = web.AppKey("identifier", object)  # pokescan.identify.Identifier, or None without Scrydex
SETTINGS = web.AppKey("settings", object)  # Settings

MAX_SETS = 6  # sets of packs being opened at once
CROP_NAME = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9]{4}\.jpg$")

PAGES = files("pokescan") / "web"


class Settings:
    """Small choices that outlive a run, such as the set being opened on stream."""

    def __init__(self, path: Path | None):
        self.path = path
        try:
            self.values = json.loads(path.read_text()) if path else {}
        except (OSError, ValueError):
            self.values = {}

    def get(self, key: str, default=None):
        return self.values.get(key, default)

    def stream_sets(self) -> list[dict]:
        """The sets of the packs being opened, as [{id, name}]; an older single choice counts as one."""
        sets = self.get("stream_sets")
        if sets is None and (single := self.get("stream_set")):
            sets = [single]
        return sets or []

    def set(self, key: str, value) -> None:
        self.values[key] = value
        if self.path:
            self.path.write_text(json.dumps(self.values, indent=2))


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


def full_state(app: web.Application) -> dict:
    state = app[STORE].state()
    state["scanner"] = app[SCANNER].snapshot() if app[SCANNER] else None
    state["identify"] = app[IDENTIFIER] is not None
    state["stream_sets"] = app[SETTINGS].stream_sets()
    last, ident = state["last"], app[IDENTIFIER]
    state["last_variants"] = ident.variants_for(last["card_id"]) if ident and last else None
    state["last_alternatives"] = ident.alternatives_for(last["id"]) if ident and last else None
    return state


async def broadcast(app: web.Application) -> None:
    message = json.dumps(full_state(app))
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
    return web.json_response(full_state(request.app))


async def socket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    request.app[SOCKETS].add(ws)
    await ws.send_str(json.dumps(full_state(request.app)))
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
            crop=crop_name(body.get("crop")),
        )
    except (ValueError, AttributeError) as e:
        raise web.HTTPBadRequest(text=str(e) or "expected a JSON object")
    if scan.crop and request.app[SCANNER]:
        request.app[SCANNER].take(scan.crop)
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
    return web.json_response(full_state(request.app))


def crop_name(value) -> str | None:
    """A crop file name from a client, or None; anything that could be a path is refused."""
    if not value:
        return None
    if not isinstance(value, str) or not CROP_NAME.match(value):
        raise ValueError("not a crop")
    return value


async def crop(request: web.Request) -> web.FileResponse:
    name = request.match_info["name"]
    crops = request.app[CROPS]
    if crops is None or not CROP_NAME.match(name) or not (crops / name).is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(crops / name, headers={"Cache-Control": "private, max-age=86400"})


async def dismiss_capture(request: web.Request) -> web.Response:
    scanner = request.app[SCANNER]
    if not scanner or not scanner.dismiss(int(request.match_info["capture_id"])):
        raise web.HTTPNotFound(text="no such capture")
    return web.json_response({"ok": True})


def identifier(request: web.Request):
    if not request.app[IDENTIFIER]:
        raise web.HTTPNotFound(text="card lookup is off: run `pokescan login` to add a Scrydex key")
    return request.app[IDENTIFIER]


async def scrydex_call(call):
    """Run a Scrydex-backed call, turning its failures into an error the dock can show."""
    from pokescan.scrydex import ScrydexError
    try:
        return await call
    except ScrydexError as e:
        raise web.HTTPBadGateway(text=str(e))


async def search_sets(request: web.Request) -> web.Response:
    found = await scrydex_call(identifier(request).scrydex.expansions(request.query.get("q", "")))
    return web.json_response(found)


async def choose_sets(request: web.Request) -> web.Response:
    """Replace the sets of the packs being opened: {sets: [{id, name}]}."""
    try:
        body = await request.json()
        chosen = [{"id": str(x["id"]), "name": str(x.get("name") or x["id"])} for x in body["sets"]][:MAX_SETS]
    except (ValueError, KeyError, TypeError) as e:
        raise web.HTTPBadRequest(text="expected {sets: [{id, name}]}") from e
    request.app[SETTINGS].set("stream_sets", list({x["id"]: x for x in chosen}.values()))
    await broadcast(request.app)
    return web.json_response(request.app[SETTINGS].stream_sets())


async def search_cards(request: web.Request) -> web.Response:
    found = await scrydex_call(identifier(request).search(request.query.get("q", "")))
    return web.json_response(found)


async def choose_card(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        call = identifier(request).choose(int(request.match_info["capture_id"]), str(body["card_id"]), str(body["variant"]))
        scan = await scrydex_call(call)
    except LookupError as e:
        raise web.HTTPNotFound(text=str(e))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise web.HTTPBadRequest(text=str(e) or "expected {card_id, variant}")
    await broadcast(request.app)
    return web.json_response(scan.to_json(), status=201)


async def change_card(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        scan = await scrydex_call(identifier(request).change_card(int(request.match_info["scan_id"]), str(body["card_id"])))
    except LookupError as e:
        raise web.HTTPNotFound(text=str(e))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise web.HTTPBadRequest(text=str(e) or "expected {card_id}")
    await broadcast(request.app)
    return web.json_response(scan.to_json())


async def change_variant(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        scan = await scrydex_call(identifier(request).change_variant(int(request.match_info["scan_id"]), str(body["variant"])))
    except LookupError as e:
        raise web.HTTPNotFound(text=str(e))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise web.HTTPBadRequest(text=str(e) or "expected {variant}")
    await broadcast(request.app)
    return web.json_response(scan.to_json())


async def run_scanner(app: web.Application):
    """Run the scanner for the server's lifetime, pushing its changes to every page."""
    scanner = app[SCANNER]
    if scanner is None:
        yield
        return
    loop = asyncio.get_running_loop()
    scanner.on_change = lambda: loop.create_task(broadcast(app))
    if app[IDENTIFIER]:
        app[IDENTIFIER].on_added = lambda scan: loop.create_task(broadcast(app))
    task = asyncio.create_task(scanner.run())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    if app[IDENTIFIER]:
        await app[IDENTIFIER].close()


async def export_csv(request: web.Request) -> web.Response:
    store = request.app[STORE]
    out = io.StringIO()
    store.export_csv(out)
    filename = f"pokescan-session-{store.session_id}-{store.started_at()[:10]}.csv"
    return web.Response(
        text=out.getvalue(), content_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def make_app(store: SessionStore, token: str, scanner=None, crops_dir: Path | None = None,
             identifier=None, settings: "Settings | None" = None) -> web.Application:
    app = web.Application(middlewares=[guard])
    app[STORE] = store
    app[TOKEN] = token
    app[SOCKETS] = weakref.WeakSet()
    app[SCANNER] = scanner
    app[CROPS] = crops_dir
    app[IDENTIFIER] = identifier
    app[SETTINGS] = settings or Settings(None)
    app.cleanup_ctx.append(run_scanner)
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
    app.router.add_get("/crops/{name}", crop)
    app.router.add_post(r"/captures/{capture_id:\d+}/dismiss", dismiss_capture)
    app.router.add_post(r"/captures/{capture_id:\d+}/choose", choose_card)
    app.router.add_post(r"/scans/{scan_id:\d+}/variant", change_variant)
    app.router.add_get("/sets", search_sets)
    app.router.add_post(r"/scans/{scan_id:\d+}/card", change_card)
    app.router.add_post("/settings/sets", choose_sets)
    app.router.add_get("/cards", search_cards)
    return app
