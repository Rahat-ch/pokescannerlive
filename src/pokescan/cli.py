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


def cmd_serve(args) -> int:
    from aiohttp import web

    from pokescan.server import load_token, make_app
    from pokescan.session import SessionStore

    args.data_dir.mkdir(parents=True, exist_ok=True)
    store = SessionStore(args.data_dir / "pokescan.db")
    token = load_token(args.data_dir)
    base = f"http://127.0.0.1:{args.port}"
    print(f"session {store.session_id}, started {store.started_at()}")
    print(f"overlay (OBS Browser Source, 1920x1080): {base}/overlay?token={token}")
    print(f"control (OBS Docks → Custom Browser Docks): {base}/control?token={token}")
    try:
        web.run_app(make_app(store, token), host="127.0.0.1", port=args.port, print=None)
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

    serve = sub.add_parser("serve", help="run the overlay and control dock for a stream")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--data-dir", type=Path, default=Path.home() / ".pokescan",
                       help="where the session database and URL token live")
    serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    if args.func is cmd_serve:
        return cmd_serve(args)
    try:
        return asyncio.run(args.func(args))
    except ObsError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
