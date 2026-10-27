# pokescan

An OBS overlay for opening Pokémon TCG cards on stream. It shows each card you pull with its price and keeps a running session total. Hold a card up to the camera and pokescan spots it, waits until it's steady and sharp, and puts a straightened image of it in the control dock; you type the name and price for now. Automatic identification and pricing come next (see [the plan](research/obs-card-scanner.md#5-recommended-architecture-and-mvp-plan)).

## Run it

Needs [uv](https://docs.astral.sh/uv/) and OBS 28 or later.

```sh
uv run pokescan serve --source Camera
```

`--source` is your OBS camera input (`uv run pokescan sources` lists them); leave it out to enter cards by hand only. The first run downloads the card detection model (108 MB) into `~/.pokescan/models/`.

It prints two URLs. Both carry a token that stays the same between runs, so you only set OBS up once.

## Set up OBS

1. **Overlay (what viewers see).** In your scene, add **Source → Browser** and paste the `overlay` URL. Set its width and height to your canvas size (Settings → Video → Base Canvas, e.g. 1920×1080 or 2560×1440); the overlay scales to fit. Put it above your camera. It is transparent apart from the total (top right) and the pulled-card pop-up (bottom left).
2. **Control dock (only you see it).** **Docks → Custom Browser Docks**, name it `pokescan`, paste the `control` URL.

Every 4 minutes a lower third slides in at the bottom right for 12 seconds (follow, the open-source repo, custom integrations), never over a pull. Add `&every=10` to the overlay URL for a gap of 10 minutes, or `&lower=off` to turn them off.

From the dock you can add a card with its price, undo the last one, remove or restore any card, export the session as CSV, and start a new session. The session is saved as you go, so it survives a crash or restart until you press **New session**.

Repeats count: a second copy of a card is added to the total and marked ×2 on stream.

**Before going live, add the packs you're opening** under *Packs I'm opening* in the dock (e.g. Prismatic Evolutions). Scanned cards are matched within those sets first, which tells same-named reprints apart and costs no lookups once the sets are fetched for the day. When the scanner still can't tell lookalikes apart, it prices its best guess and the dock offers the others under **Last card**, one click to swap.

## Sounds

When a card's price lands on stream, a Pokémon card plays its Pokémon's cry: the original-style one for Gen 1–5, the current one for newer Pokémon. Trainers and Energy play little 8-bit sounds for their kind (Supporter, Item or Tool, Stadium, Energy), and typed-in cards a cash ping.

- For viewers to hear them, the overlay's Browser Source needs **Control audio via OBS** ticked. It then gets its own slider in the Audio Mixer.
- Cries are downloaded from [PokéAPI's collection](https://github.com/PokeAPI/cries) the first time each Pokémon is pulled and kept in `~/.pokescan/cries/`. They're the games' audio, so they aren't part of this repo.
- To use your own sound for any kind, put a file named `supporter`, `item`, `stadium`, `energy` or `cash` (`.ogg`, `.mp3`, `.wav` or `.m4a`) in `~/.pokescan/sounds/`.

## Data

Everything lives in `~/.pokescan/` (change with `--data-dir`): the session database `pokescan.db` and the URL token. The server only listens on 127.0.0.1.

## Other commands

- `uv run pokescan sources` lists OBS inputs, marking cameras.
- `uv run pokescan bench --source Camera` measures how fast OBS can hand frames to pokescan, and what it costs OBS ([results](research/obs-card-scanner.md#54-open-risks)).

## Development

```sh
uv run pytest
```

## License

Apache-2.0; see [LICENSE](LICENSE). Third-party credits, including the card detection model from [Cardstream](https://github.com/Ximilar-com/cardstream), are in [NOTICE](NOTICE).
