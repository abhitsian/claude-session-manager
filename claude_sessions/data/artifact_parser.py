import json
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..config import settings
from .models import Artifact


class ArtifactParser:
    """Parser for Claude Code artifacts (files created/modified)."""

    def __init__(self, claude_dir: Optional[Path] = None):
        self.claude_dir = claude_dir or settings.claude_data_dir
        self.projects_dir = self.claude_dir / "projects"
        self.file_history_dir = self.claude_dir / "file-history"

    def get_all_artifacts(self, limit: int = 100) -> List[Artifact]:
        """Get all artifacts across all sessions, sorted by recency."""
        artifacts = []
        seen_paths = {}  # Track latest version of each file

        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            for session_file in project_dir.glob("*.jsonl"):
                if session_file.stem.startswith("agent-"):
                    continue

                session_id = session_file.stem
                session_artifacts = self._parse_session_artifacts(
                    session_file, session_id
                )

                for artifact in session_artifacts:
                    key = artifact.file_path
                    if key not in seen_paths or artifact.timestamp > seen_paths[key].timestamp:
                        seen_paths[key] = artifact

        artifacts = list(seen_paths.values())
        artifacts.sort(key=lambda a: a.timestamp, reverse=True)
        return artifacts[:limit]

    def get_session_artifacts(self, session_id: str) -> List[Artifact]:
        """Get all artifacts for a specific session."""
        session_file = self._find_session_file(session_id)
        if not session_file:
            return []

        return self._parse_session_artifacts(session_file, session_id)

    def _find_session_file(self, session_id: str) -> Optional[Path]:
        """Find the session file for a given session ID."""
        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            session_file = project_dir / f"{session_id}.jsonl"
            if session_file.exists():
                return session_file
        return None

    def _parse_session_artifacts(
        self, session_file: Path, session_id: str
    ) -> List[Artifact]:
        """Parse artifacts from a session JSONL file."""
        artifacts = []
        seen = set()

        with open(session_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check for toolUseResult with file operations
                tool_result = obj.get("toolUseResult")
                if tool_result and isinstance(tool_result, dict):
                    file_path = tool_result.get("filePath")
                    if file_path and file_path not in seen:
                        seen.add(file_path)
                        artifact = self._create_artifact_from_tool_result(
                            tool_result, obj, session_id
                        )
                        if artifact:
                            artifacts.append(artifact)

                # Also check for tool_use in assistant messages (for context)
                message = obj.get("message", {})
                content = message.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})

                            if tool_name in ("Write", "Edit") and isinstance(tool_input, dict):
                                file_path = tool_input.get("file_path")
                                if file_path and file_path not in seen:
                                    seen.add(file_path)
                                    artifact = self._create_artifact_from_tool_use(
                                        tool_name, tool_input, obj, session_id
                                    )
                                    if artifact:
                                        artifacts.append(artifact)

        return artifacts

    def _create_artifact_from_tool_result(
        self, tool_result: Dict[str, Any], obj: Dict[str, Any], session_id: str
    ) -> Optional[Artifact]:
        """Create an Artifact from a toolUseResult entry."""
        file_path = tool_result.get("filePath")
        if not file_path:
            return None

        operation = tool_result.get("type", "unknown")
        content = tool_result.get("content", "")
        timestamp_str = obj.get("timestamp")

        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.now()

        return Artifact(
            file_path=file_path,
            file_name=Path(file_path).name,
            file_type=self._get_file_type(file_path),
            operation=operation,
            session_id=session_id,
            timestamp=timestamp,
            size_bytes=len(content) if content else 0,
            mime_type=self._get_mime_type(file_path),
            exists=Path(file_path).exists(),
        )

    def _create_artifact_from_tool_use(
        self,
        tool_name: str,
        tool_input: Dict[str, Any],
        obj: Dict[str, Any],
        session_id: str,
    ) -> Optional[Artifact]:
        """Create an Artifact from a tool_use block."""
        file_path = tool_input.get("file_path")
        if not file_path:
            return None

        operation = "create" if tool_name == "Write" else "edit"
        content = tool_input.get("content", "")
        timestamp_str = obj.get("timestamp")

        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            timestamp = datetime.now()

        return Artifact(
            file_path=file_path,
            file_name=Path(file_path).name,
            file_type=self._get_file_type(file_path),
            operation=operation,
            session_id=session_id,
            timestamp=timestamp,
            size_bytes=len(content) if content else 0,
            mime_type=self._get_mime_type(file_path),
            exists=Path(file_path).exists(),
        )

    def _get_file_type(self, file_path: str) -> str:
        """Determine the file type category."""
        ext = Path(file_path).suffix.lower()

        type_map = {
            # Code
            ".py": "code",
            ".js": "code",
            ".ts": "code",
            ".tsx": "code",
            ".jsx": "code",
            ".java": "code",
            ".go": "code",
            ".rs": "code",
            ".c": "code",
            ".cpp": "code",
            ".h": "code",
            ".rb": "code",
            ".php": "code",
            ".swift": "code",
            ".kt": "code",
            ".scala": "code",
            ".r": "code",
            # Web
            ".html": "web",
            ".css": "web",
            ".scss": "web",
            ".less": "web",
            ".vue": "web",
            ".svelte": "web",
            # Config
            ".json": "config",
            ".yaml": "config",
            ".yml": "config",
            ".toml": "config",
            ".ini": "config",
            ".env": "config",
            ".xml": "config",
            ".plist": "config",
            # Documents
            ".md": "document",
            ".txt": "document",
            ".rst": "document",
            ".org": "document",
            # Shell
            ".sh": "shell",
            ".bash": "shell",
            ".zsh": "shell",
            ".fish": "shell",
            # Data
            ".csv": "data",
            ".sql": "data",
            ".db": "data",
            # Images
            ".png": "image",
            ".jpg": "image",
            ".jpeg": "image",
            ".gif": "image",
            ".svg": "image",
            ".webp": "image",
        }

        return type_map.get(ext, "other")

    def _get_mime_type(self, file_path: str) -> str:
        """Get the MIME type for a file."""
        mime_type, _ = mimetypes.guess_type(file_path)
        return mime_type or "application/octet-stream"

    def get_artifact_content(self, file_path: str) -> Optional[str]:
        """Get the current content of an artifact file."""
        path = Path(file_path)
        if not path.exists():
            return None

        try:
            # Only read text files
            if self._get_file_type(file_path) in ("image", "data"):
                return None
            return path.read_text()[:10000]  # Limit to 10KB
        except Exception:
            return None

    def get_artifact_stats(self) -> Dict[str, Any]:
        """Get aggregate statistics about artifacts."""
        artifacts = self.get_all_artifacts(limit=1000)

        by_type = {}
        by_session = {}
        total_size = 0

        for a in artifacts:
            by_type[a.file_type] = by_type.get(a.file_type, 0) + 1
            by_session[a.session_id] = by_session.get(a.session_id, 0) + 1
            total_size += a.size_bytes

        return {
            "total_artifacts": len(artifacts),
            "by_type": by_type,
            "sessions_with_artifacts": len(by_session),
            "total_size_bytes": total_size,
        }
