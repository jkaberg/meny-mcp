"""Where a week's run keeps its state.

On disk, not in session memory. One run spans thirty-odd tool calls, an
approval gate the user may answer an hour later, and sometimes a restart; and
the whole point of the manifest is that the run survives losing its context.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from ..config import settings

INVENTORY = "inventory.json"
MANIFEST = "manifest.json"


def _dir() -> pathlib.Path:
    path = pathlib.Path(settings.state_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read(name: str) -> dict | None:
    path = _dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _write(name: str, payload: Any) -> str:
    path = _dir() / name
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return str(path)


def load_inventory() -> dict | None:
    return _read(INVENTORY)


def save_inventory(payload: dict) -> str:
    return _write(INVENTORY, payload)


def load_manifest() -> dict | None:
    return _read(MANIFEST)


def save_manifest(payload: dict) -> str:
    """Keep the live manifest and an archive copy under its week.

    The archive is what makes "what did we eat in August" answerable later,
    and costs one extra file per week.
    """
    week = payload.get("week_start") or "week"
    _write(f"{week}-menu.json", payload)
    return _write(MANIFEST, payload)
