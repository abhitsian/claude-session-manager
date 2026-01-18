from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Set

from ..config import settings


class ActiveSessionDetector:
    """Detect currently active Claude Code sessions."""

    def __init__(self, claude_dir: Optional[Path] = None):
        self.claude_dir = claude_dir or settings.claude_data_dir
        self.debug_dir = self.claude_dir / "debug"
        self.threshold_minutes = settings.active_threshold_minutes

    def get_latest_session_id(self) -> Optional[str]:
        """Get the session ID from the debug/latest symlink."""
        latest = self.debug_dir / "latest"
        if latest.exists() and latest.is_symlink():
            try:
                target = latest.resolve()
                return target.stem  # Remove .txt extension
            except Exception:
                pass
        return None

    def get_active_sessions(self) -> List[str]:
        """Get list of currently active session IDs."""
        active: Set[str] = set()

        # Check for recently modified debug files
        cutoff = datetime.now() - timedelta(minutes=self.threshold_minutes)

        if self.debug_dir.exists():
            for debug_file in self.debug_dir.glob("*.txt"):
                # Skip symlinks
                if debug_file.is_symlink():
                    continue

                try:
                    mtime = datetime.fromtimestamp(debug_file.stat().st_mtime)
                    if mtime > cutoff:
                        active.add(debug_file.stem)
                except Exception:
                    continue

        # Also include the latest session
        latest = self.get_latest_session_id()
        if latest:
            active.add(latest)

        return list(active)

    def is_session_active(self, session_id: str) -> bool:
        """Check if a specific session is currently active."""
        # Check latest symlink
        if session_id == self.get_latest_session_id():
            return True

        # Check debug file modification time
        debug_file = self.debug_dir / f"{session_id}.txt"
        if debug_file.exists():
            try:
                cutoff = datetime.now() - timedelta(minutes=self.threshold_minutes)
                mtime = datetime.fromtimestamp(debug_file.stat().st_mtime)
                return mtime > cutoff
            except Exception:
                pass

        return False
