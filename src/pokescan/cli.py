import argparse
import asyncio
import sys
from pathlib import Path

from pokescan.bench import format_report, full_res_shots, run_phase
from pokescan.config import obs_settings
from pokescan.obs import Obs, ObsError

# Input kinds that are cameras, on macOS, Windows and Linux.
CAMERA_KINDS = {"macos-avcapture", "macos-avcapture-fast", "av_capture_input", "av_capture_input_v2",
                "dshow_input", "v4l2_input"}


async def cmd_sources(args) -> int:
    async with Obs(obs_settings()) as obs:
        for i in await obs.inputs():
            camera = "  <- camera" if i["inputKind"] in CAMERA_KINDS else ""
            print(f"{i['inputName']!r:<40} {i['inputKind']}{camera}")
    return 0


async def cmd_bench(args) -> int:
    rates = [float(r) for r in args.rates.split(",")]
    async with Obs(obs_settings()) as obs:
        version = await obs.request("GetVersion")
        context = {
            "source": args.source,
            "obs_version": version["obsVersion"],
            "ws_version": version["obsWebSocketVersion"],
            "streaming": (await obs.request("GetStreamStatus"))["outputActive"],
            "recording": (await obs.request("GetRecordStatus"))["outputActive"],
        }
        # Fail fast on a wrong source name, before the idle phase wastes time.
        try:
            sample = await obs.screenshot(args.source, width=args.width)
        except ObsError as e:
            if "(702)" in str(e):
                raise ObsError(
                    f"OBS has no video from {args.source!r} to screenshot. "
                    "Is the camera switched on and showing in OBS's preview?"
                ) from e
            raise

        if args.save_samples:
            args.save_samples.mkdir(parents=True, exist_ok=True)
            (args.save_samples / f"poll-{args.width}.jpg").write_bytes(sample)
            (args.save_samples / "full-res.jpg").write_bytes(await obs.screenshot(args.source))

        results = []
        print(f"idle baseline, {args.seconds:g}s...", file=sys.stderr)
        results.append(await run_phase(obs, "idle", args.seconds))
        for hz in rates:
            print(f"polling at {hz:g} Hz, {args.width}px, {args.seconds:g}s...", file=sys.stderr)
            results.append(await run_phase(obs, f"{hz:g} Hz", args.seconds, args.source, hz, args.width))
        if args.full_res_shots:
            print(f"{args.full_res_shots} full-res shots...", file=sys.stderr)
            results.append(await full_res_shots(obs, args.source, args.full_res_shots))

    print(format_report(results, context))
    return 0


async def cmd_capture(args) -> int:
    """Save full-resolution frames of a source, for tuning detection and the eval set."""
    import time

    args.out.mkdir(parents=True, exist_ok=True)
    async with Obs(obs_settings()) as obs:
        count = round(args.seconds * args.hz)
        print(f"capturing {count} frames of {args.source!r} into {args.out}...", file=sys.stderr)
        start = time.monotonic()
        for i in range(count):
            await asyncio.sleep(max(0.0, start + i / args.hz - time.monotonic()))
            (args.out / f"{args.prefix}{i:04d}.jpg").write_bytes(await obs.screenshot(args.source, quality=92))
    print(f"saved {count} frames", file=sys.stderr)
    return 0


def cmd_replay(args) -> int:
    """Run the scanner's decisions over captured frames, to tune it without a camera."""
    import cv2

    from pokescan.detect import CardDetector, ensure_model
    from pokescan.scanner import replay

    frames = sorted(args.frames.glob("*.jpg"))
    detector = CardDetector(ensure_model(args.data_dir))
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    fired = 0
    for i, status, verdict, crop in replay(detector, frames, args.hz):
        mark = f"  <- {verdict.value}" if verdict else ""
        print(f"{frames[i].name}  {status:<18}{mark}")
        if crop is not None:
            fired += 1
            if args.out:
                cv2.imwrite(str(args.out / f"scan-{fired:02d}-{frames[i].stem}.jpg"), crop)
    print(f"{fired} scans from {len(frames)} frames")
    return 0


def cmd_login(args) -> int:
    """Ask for the Scrydex key and team ID without echoing them, and store them privately."""
    import getpass

    from pokescan import credentials

    print("Find these in your Scrydex dashboard. Nothing you type is shown.")
    key = getpass.getpass("Scrydex API key: ").strip()
    team = getpass.getpass("Scrydex team ID: ").strip()
    if not key or not team:
        print("error: both are needed", file=sys.stderr)
        return 1
    path = credentials.save_scrydex(args.data_dir, key, team)
    print(f"saved to {path} (readable only by you). Restart `pokescan serve` to use it.")
    return 0


def cmd_serve(args) -> int:
    import logging

    from aiohttp import web

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(asctime)s %(message)s")
    sys.stdout.reconfigure(line_buffering=True)  # startup status shows up even when output goes to a file

    from pokescan.server import load_token, make_app
    from pokescan.session import SessionStore
    from pokescan.sounds import Cries

    args.data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(args.data_dir / "pokescan.db")
    token = load_token(args.data_dir)
    base = f"http://127.0.0.1:{args.port}"
    from pokescan.server import Settings

    settings = Settings(args.data_dir / "settings.json")
    scanner = identifier = None
    crops_dir = args.data_dir / "crops"
    if args.source:
        from pokescan import credentials, ocr
        from pokescan.detect import CardDetector, ensure_model
        from pokescan.scanner import Scanner

        scanner = Scanner(obs_settings(), args.source, CardDetector(ensure_model(args.data_dir)), crops_dir,
                          rotate=args.rotate)
        print(f"scanning OBS source {args.source!r}" + (f", turned {args.rotate}°" if args.rotate else ""))
        keys = credentials.scrydex(args.data_dir)
        if keys and ocr.available():
            from pokescan.identify import Identifier
            from pokescan.scrydex import Cache, Scrydex

            from pokescan.variants import VariantJudge

            stream_sets = lambda: [x["id"] for x in settings.stream_sets()]
            judge = VariantJudge.from_env()
            identifier = Identifier(Scrydex(*keys, cache=Cache(args.data_dir / "scrydex-cache.db")),
                                    store, scanner, crops_dir, stream_sets, images_dir=args.data_dir / "card-images",
                                    judge=judge)
            print("identifying scanned cards (read on this Mac) and pricing them with Scrydex")
            print("telling holo from normal with Claude Haiku via OpenRouter" if judge else
                  "printing check off: set OPENROUTER_API_KEY to tell holo from normal; standard printing assumed")
        elif not keys:
            print("identification off: run `pokescan login` to add a Scrydex key; scans wait to be typed in")
        else:
            print("identification off: reading card text needs macOS; scans wait to be typed in")
    else:
        print("scanning off: add --source <OBS camera input> to scan held-up cards")
    print(f"session {store.session_id}, started {store.started_at()}")
    print(f"overlay (OBS Browser Source, at your canvas size): {base}/overlay?token={token}")
    print(f"control (OBS Docks → Custom Browser Docks): {base}/control?token={token}")
    print(f"sounds: Pokémon cries download to {args.data_dir / 'cries'}; your own files go in {args.data_dir / 'sounds'}")
    app = make_app(store, token, scanner, crops_dir, identifier, settings,
                   cries=Cries(args.data_dir / "cries"), sounds_dir=args.data_dir / "sounds")
    try:
        web.run_app(app, host="127.0.0.1", port=args.port, print=None)
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pokescan")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sources", help="list OBS inputs, marking cameras").set_defaults(func=cmd_sources)

    bench = sub.add_parser("bench", help="time GetSourceScreenshot and its cost to OBS")
    bench.add_argument("--source", required=True, help="OBS input to screenshot (see `pokescan sources`)")
    bench.add_argument("--rates", default="2,5,10", help="polling rates in Hz, comma-separated")
    bench.add_argument("--width", type=int, default=640, help="poll screenshot width in px")
    bench.add_argument("--seconds", type=float, default=15, help="length of each phase")
    bench.add_argument("--full-res-shots", type=int, default=10, help="native-resolution shots to time")
    bench.add_argument("--save-samples", type=Path, help="directory to save one poll and one full-res frame")
    bench.set_defaults(func=cmd_bench)

    capture = sub.add_parser("capture", help="save full-resolution frames of a source")
    capture.add_argument("--source", required=True)
    capture.add_argument("--out", type=Path, required=True)
    capture.add_argument("--seconds", type=float, default=10)
    capture.add_argument("--hz", type=float, default=2)
    capture.add_argument("--prefix", default="frame-")
    capture.set_defaults(func=cmd_capture)

    replay_ = sub.add_parser("replay", help="run the scanner over captured frames")
    replay_.add_argument("frames", type=Path, help="directory of frames from `pokescan capture`")
    replay_.add_argument("--hz", type=float, default=2, help="rate the frames were captured at")
    replay_.add_argument("--out", type=Path, help="directory to save the scanned crops")
    replay_.add_argument("--data-dir", type=Path, default=Path.home() / ".pokescan")
    replay_.set_defaults(func=cmd_replay)

    login = sub.add_parser("login", help="store your Scrydex API key and team ID")
    login.add_argument("--data-dir", type=Path, default=Path.home() / ".pokescan")
    login.set_defaults(func=cmd_login)

    serve = sub.add_parser("serve", help="run the overlay and control dock for a stream")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--data-dir", type=Path, default=Path.home() / ".pokescan",
                       help="where the session database and URL token live")
    serve.add_argument("--verbose", action="store_true", help="log every request")
    serve.add_argument("--source", help="OBS camera input to scan for held-up cards (see `pokescan sources`)")
    serve.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=0,
                       help="turn the camera's frames clockwise before scanning, for a camera mounted sideways or upside down")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    if args.func in (cmd_serve, cmd_replay, cmd_login):
        return args.func(args)
    try:
        return asyncio.run(args.func(args))
    except ObsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
