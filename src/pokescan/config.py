"""Where to find OBS's WebSocket server and its password.

Precedence: POKESCAN_OBS_URL / POKESCAN_OBS_PASSWORD, then obs-websocket's own
config.json (so a local setup needs no configuration), then OBS's defaults.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 4455


def obs_websocket_config_paths() -> list[Path]:
    """obs-websocket's config.json on macOS, Linux and Windows."""
    home = Path.home()
    paths = [
        home / "Library/Application Support/obs-studio/plugin_config/obs-websocket/config.json",
        home / ".config/obs-studio/plugin_config/obs-websocket/config.json",
    ]
    if appdata := os.environ.get("APPDATA"):
        paths.append(Path(appdata) / "obs-studio/plugin_config/obs-websocket/config.json")
    return paths


@dataclass(frozen=True)
class ObsSettings:
    url: str
    password: str
    source: str  # human-readable origin of the settings, for error messages

    def __repr__(self) -> str:
        # Never print the password.
        return f"ObsSettings(url={self.url!r}, password={'<set>' if self.password else '<none>'}, source={self.source!r})"


def obs_settings(env: dict[str, str] | None = None, config_paths: list[Path] | None = None) -> ObsSettings:
    env = os.environ if env is None else env
    config_paths = obs_websocket_config_paths() if config_paths is None else config_paths

    url = env.get("POKESCAN_OBS_URL")
    password = env.get("POKESCAN_OBS_PASSWORD")
    if url is not None and password is not None:
        return ObsSettings(url, password, "environment")

    for path in config_paths:
        try:
            config = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        port = config.get("server_port", DEFAULT_PORT)
        file_password = config.get("server_password", "") if config.get("auth_required", True) else ""
        return ObsSettings(
            url if url is not None else f"ws://127.0.0.1:{port}",
            password if password is not None else file_password,
            str(path),
        )

    return ObsSettings(url or f"ws://127.0.0.1:{DEFAULT_PORT}", password or "", "defaults")
