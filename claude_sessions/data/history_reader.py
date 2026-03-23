"""Read Claude Code's history.jsonl for metadata about ALL sessions, including deleted ones."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Optional

from ..config import settings
from .session_parser import _generate_title


HISTORY_FILE = settings.claude_data_dir / "history.jsonl"


def get_all_session_history() -> Dict[str, dict]:
    """Parse history.jsonl and return metadata for every session ever.

    Returns dict keyed by session_id with:
        - session_id
        - title (generated from first prompt)
        - prompts (list of user prompts)
        - project_path
        - start_time
        - last_activity
        - message_count (number of history entries)
    """
    if not HISTORY_FILE.exists():
        return {}

    sessions: Dict[str, dict] = {}

    with open(HISTORY_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            sid = obj.get("sessionId", "")
            if not sid:
                continue

            ts_ms = obj.get("timestamp", 0)
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc) if ts_ms else None
            display = obj.get("display", "").strip()
            project = obj.get("project", "")

            if sid not in sessions:
                sessions[sid] = {
                    "session_id": sid,
                    "prompts": [],
                    "project_path": project,
                    "start_time": ts,
                    "last_activity": ts,
                    "title": None,
                }

            entry = sessions[sid]
            if display:
                entry["prompts"].append(display)
            if ts:
                if entry["start_time"] is None or ts < entry["start_time"]:
                    entry["start_time"] = ts
                if entry["last_activity"] is None or ts > entry["last_activity"]:
                    entry["last_activity"] = ts

    # Generate titles from first prompt
    for sid, entry in sessions.items():
        if entry["prompts"]:
            entry["title"] = _generate_title(entry["prompts"][0])
        else:
            entry["title"] = f"Session {sid[:8]}"
        entry["message_count"] = len(entry["prompts"])

    return sessions


def get_deleted_sessions() -> List[dict]:
    """Get sessions that exist in history but have no JSONL file."""
    all_sessions = get_all_session_history()
    projects_dir = settings.claude_data_dir / "projects"

    deleted = []
    for sid, entry in all_sessions.items():
        found = False
        if projects_dir.exists():
            for d in projects_dir.iterdir():
                if d.is_dir() and (d / f"{sid}.jsonl").exists():
                    found = True
                    break
        if not found:
            deleted.append(entry)

    deleted.sort(key=lambda e: e["last_activity"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return deleted
