"""API keys, kept out of the repo and out of the shell history.

SCRYDEX_API_KEY / SCRYDEX_TEAM_ID in the environment win; otherwise
`pokescan login` stores them in <data dir>/credentials.json, readable only
by the user.
"""

import json
import os
from pathlib import Path


def credentials_path(data_dir: Path) -> Path:
    return data_dir / "credentials.json"


def load(data_dir: Path) -> dict:
    try:
        return json.loads(credentials_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}


def scrydex(data_dir: Path, env: dict | None = None) -> tuple[str, str] | None:
    """(api key, team id), or None when Scrydex isn't set up."""
    env = os.environ if env is None else env
    stored = load(data_dir).get("scrydex", {})
    key = env.get("SCRYDEX_API_KEY") or stored.get("api_key")
    team = env.get("SCRYDEX_TEAM_ID") or stored.get("team_id")
    return (key, team) if key and team else None


def save_scrydex(data_dir: Path, api_key: str, team_id: str) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = credentials_path(data_dir)
    stored = load(data_dir)
    stored["scrydex"] = {"api_key": api_key.strip(), "team_id": team_id.strip()}
    # Create it private before writing the secret into it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(stored, f, indent=2)
    path.chmod(0o600)
    return path
