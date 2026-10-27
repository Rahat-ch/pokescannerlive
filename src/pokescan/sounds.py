"""Sounds on stream: each Pokémon card plays its cry, other cards a sound for their kind.

Cries come from PokéAPI's collection (github.com/PokeAPI/cries), downloaded the
first time a Pokémon is pulled and kept in the data directory; they're the
games' audio, so they're never part of this repo. The original-style cry is
used where there is one (Gen 1-5), else the current one.

Trainers, Energy and typed-in cards play sounds the overlay makes itself, unless
a file of that name is dropped into the sounds folder (e.g. sounds/supporter.ogg).
"""

import asyncio
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

log = logging.getLogger("pokescan.sounds")

CRIES = "https://raw.githubusercontent.com/PokeAPI/cries/main/cries/pokemon"
LAST_LEGACY = 649  # the original-style cries cover Gen 1-5
MAX_DEX = 2000
# Sounds the overlay plays when no Pokémon cry applies; each can be replaced by a file.
NAMES = ("supporter", "item", "stadium", "energy", "cash")
EXTENSIONS = (".ogg", ".mp3", ".wav", ".m4a")


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "pokescan"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


def custom(sounds_dir: Path | None) -> dict[str, str]:
    """The streamer's own sound files, by name: {'supporter': 'supporter.mp3'}."""
    found = {}
    for name in NAMES:
        for ext in EXTENSIONS:
            if sounds_dir and (sounds_dir / f"{name}{ext}").is_file():
                found[name] = f"{name}{ext}"
                break
    return found


class Cries:
    def __init__(self, cries_dir: Path, fetch: Callable[[str], bytes] = download):
        self.dir = cries_dir
        self.fetch = fetch
        self._lock = asyncio.Lock()

    async def path(self, dex: int) -> Path | None:
        """The cry for a National Pokédex number, downloaded once; None if there isn't one."""
        if not 1 <= dex <= MAX_DEX:
            return None
        path = self.dir / f"{dex}.ogg"
        async with self._lock:  # two overlays asking at once download it once
            if path.exists():
                return path
            for kind in (("legacy", "latest") if dex <= LAST_LEGACY else ("latest",)):
                try:
                    data = await asyncio.to_thread(self.fetch, f"{CRIES}/{kind}/{dex}.ogg")
                except (urllib.error.URLError, OSError) as e:
                    log.info("no %s cry for #%d: %s", kind, dex, e)
                    continue
                self.dir.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                return path
        return None
