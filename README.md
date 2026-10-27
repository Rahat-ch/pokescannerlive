# pokescan

An OBS overlay for opening Pokémon TCG cards on stream. It shows each card you pull with its price and keeps a running session total. Cards are entered by hand for now; camera identification and automatic pricing come next (see [the plan](research/obs-card-scanner.md#5-recommended-architecture-and-mvp-plan)).

## Run it

Needs [uv](https://docs.astral.sh/uv/) and OBS 28 or later.

```sh
uv run pokescan serve
```

It prints two URLs. Both carry a token that stays the same between runs, so you only set OBS up once.

## Set up OBS

1. **Overlay (what viewers see).** In your scene, add **Source → Browser** and paste the `overlay` URL. Set its width and height to your canvas size (Settings → Video → Base Canvas, e.g. 1920×1080 or 2560×1440); the overlay scales to fit. Put it above your camera. It is transparent apart from the total (top right) and the pulled-card pop-up (bottom left).
2. **Control dock (only you see it).** **Docks → Custom Browser Docks**, name it `pokescan`, paste the `control` URL.

From the dock you can add a card with its price, undo the last one, remove or restore any card, export the session as CSV, and start a new session. The session is saved as you go, so it survives a crash or restart until you press **New session**.

Repeats count: a second copy of a card is added to the total and marked ×2 on stream.

## Data

Everything lives in `~/.pokescan/` (change with `--data-dir`): the session database `pokescan.db` and the URL token. The server only listens on 127.0.0.1.

## Other commands

- `uv run pokescan sources` lists OBS inputs, marking cameras.
- `uv run pokescan bench --source Camera` measures how fast OBS can hand frames to pokescan, and what it costs OBS ([results](research/obs-card-scanner.md#54-open-risks)).

## Development

```sh
uv run pytest
```
