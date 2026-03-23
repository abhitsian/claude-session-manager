"""Manage session favorites using a local JSON file."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from ..config import settings

FAVORITES_FILE = settings.claude_data_dir / "session-favorites.json"


def _read_store() -> dict:
    """Read the favorites JSON file, returning empty structure if missing or corrupt."""
    try:
        return json.loads(FAVORITES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"favorites": {}}


def _write_store(data: dict) -> None:
    """Atomically write the favorites JSON file using rename."""
    FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=FAVORITES_FILE.parent, suffix=".tmp", prefix=".favorites-"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, FAVORITES_FILE)
    except BaseException:
        # Clean up temp file on any failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_favorite(session_id: str) -> bool:
    """Check whether a session is favorited."""
    store = _read_store()
    return session_id in store.get("favorites", {})


def toggle_favorite(session_id: str, label: str = "") -> bool:
    """Toggle the favorite state of a session.

    Returns True if the session is now favorited, False if unfavorited.
    """
    store = _read_store()
    favorites = store.setdefault("favorites", {})

    if session_id in favorites:
        del favorites[session_id]
        _write_store(store)
        return False

    favorites[session_id] = {
        "starred_at": datetime.now(timezone.utc).isoformat(),
        "label": label,
    }
    _write_store(store)
    return True


def get_favorites() -> List[dict]:
    """Return all favorites as a list of dicts with session_id, starred_at, label."""
    store = _read_store()
    return [
        {
            "session_id": sid,
            "starred_at": entry.get("starred_at", ""),
            "label": entry.get("label", ""),
        }
        for sid, entry in store.get("favorites", {}).items()
    ]


def set_label(session_id: str, label: str) -> None:
    """Update the label for an existing favorite.

    Raises KeyError if the session is not currently favorited.
    """
    store = _read_store()
    favorites = store.get("favorites", {})

    if session_id not in favorites:
        raise KeyError(f"Session {session_id!r} is not a favorite")

    favorites[session_id]["label"] = label
    _write_store(store)
