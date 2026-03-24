import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

from ..config import settings


class ActiveSessionDetector:
    """Detect currently active Claude Code sessions.

    Uses three signals:
    1. JSONL file modification time (recently written = active)
    2. Running `claude` processes (extract session IDs from args)
    3. Debug file modification time (legacy, still works for some versions)
    """

    def __init__(self, claude_dir: Optional[Path] = None):
        self.claude_dir = claude_dir or settings.claude_data_dir
        self.projects_dir = self.claude_dir / "projects"
        self.debug_dir = self.claude_dir / "debug"
        self.threshold_minutes = settings.active_threshold_minutes

    def get_latest_session_id(self) -> Optional[str]:
        """Get the session ID from the debug/latest symlink."""
        latest = self.debug_dir / "latest"
        if latest.exists() and latest.is_symlink():
            try:
                target = latest.resolve()
                return target.stem
            except Exception:
                pass
        return None

    def get_active_sessions(self) -> List[str]:
        """Get list of currently active session IDs."""
        active: Set[str] = set()
        cutoff = datetime.now() - timedelta(minutes=self.threshold_minutes)

        # 1. Check JSONL files modified recently (most reliable signal)
        if self.projects_dir.exists():
            for project_dir in self.projects_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for jsonl in project_dir.glob("*.jsonl"):
                    if jsonl.stem.startswith("agent-"):
                        continue
                    try:
                        mtime = datetime.fromtimestamp(jsonl.stat().st_mtime)
                        if mtime > cutoff:
                            active.add(jsonl.stem)
                    except Exception:
                        continue

        # 2. Check running claude processes
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                if "claude" not in line.lower():
                    continue
                # Match: claude --resume <session-id>
                resume_match = re.search(
                    r"claude\s+--resume\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    line
                )
                if resume_match:
                    active.add(resume_match.group(1))
                # Match: sessionId in the process args
                session_match = re.search(
                    r"sessionId[=:]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                    line
                )
                if session_match:
                    active.add(session_match.group(1))
        except Exception:
            pass

        # 3. Legacy: debug file modification time
        if self.debug_dir.exists():
            for debug_file in self.debug_dir.glob("*.txt"):
                if debug_file.is_symlink():
                    continue
                try:
                    mtime = datetime.fromtimestamp(debug_file.stat().st_mtime)
                    if mtime > cutoff:
                        active.add(debug_file.stem)
                except Exception:
                    continue

        # 4. Include the latest session
        latest = self.get_latest_session_id()
        if latest:
            active.add(latest)

        return list(active)

    def is_session_active(self, session_id: str) -> bool:
        """Check if a specific session is currently active."""
        return session_id in set(self.get_active_sessions())
