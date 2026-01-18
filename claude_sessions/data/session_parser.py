import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Iterator, Dict, Any

from ..config import settings
from .models import SessionMetadata, ConversationMessage, SessionStats, TodoItem


class SessionParser:
    """Parser for Claude Code session data files."""

    def __init__(self, claude_dir: Optional[Path] = None):
        self.claude_dir = claude_dir or settings.claude_data_dir
        self.projects_dir = self.claude_dir / "projects"
        self.history_file = self.claude_dir / "history.jsonl"
        self.stats_file = self.claude_dir / "stats-cache.json"
        self.todos_dir = self.claude_dir / "todos"

    def get_all_sessions(self) -> List[SessionMetadata]:
        """Get all sessions with metadata, sorted by last activity."""
        sessions = []
        seen_ids = set()

        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue

            for session_file in project_dir.glob("*.jsonl"):
                # Skip agent files
                if session_file.stem.startswith("agent-"):
                    continue

                session_id = session_file.stem
                if session_id in seen_ids:
                    continue
                seen_ids.add(session_id)

                metadata = self._parse_session_metadata(session_file, project_dir.name)
                if metadata:
                    sessions.append(metadata)

        return sorted(sessions, key=lambda s: s.last_activity, reverse=True)

    def _parse_session_metadata(
        self, file_path: Path, project_name: str
    ) -> Optional[SessionMetadata]:
        """Parse a session JSONL file to extract metadata."""
        try:
            messages = list(self._stream_messages(file_path))
            if not messages:
                return None

            # Extract timestamps
            timestamps = [m.timestamp for m in messages]
            start_time = min(timestamps)
            last_activity = max(timestamps)

            # Count messages
            user_count = sum(1 for m in messages if m.type == "user")
            assistant_count = sum(1 for m in messages if m.type == "assistant")

            # Find model and tokens
            model_used = None
            total_input = 0
            total_output = 0
            for m in messages:
                if m.model:
                    model_used = m.model
                if m.token_usage:
                    total_input += m.token_usage.get("input_tokens", 0)
                    total_output += m.token_usage.get("output_tokens", 0)

            # Extract summaries
            summaries = [m.content for m in messages if m.type == "summary"]

            # Decode project path
            project_path = project_name.replace("-", "/")
            if not project_path.startswith("/"):
                project_path = "/" + project_path

            return SessionMetadata(
                session_id=file_path.stem,
                project_path=project_path,
                start_time=start_time,
                last_activity=last_activity,
                message_count=len(messages),
                user_message_count=user_count,
                assistant_message_count=assistant_count,
                model_used=model_used,
                total_input_tokens=total_input,
                total_output_tokens=total_output,
                summaries=summaries[:3],  # Keep first 3 summaries
                file_path=str(file_path),
            )
        except Exception as e:
            print(f"Error parsing {file_path}: {e}")
            return None

    def _stream_messages(self, file_path: Path) -> Iterator[ConversationMessage]:
        """Stream messages from a session JSONL file."""
        with open(file_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    msg = self._parse_message(obj)
                    if msg:
                        yield msg
                except json.JSONDecodeError:
                    continue

    def _parse_message(self, obj: Dict[str, Any]) -> Optional[ConversationMessage]:
        """Parse a single message object."""
        msg_type = obj.get("type")
        if msg_type not in ("user", "assistant", "summary"):
            return None

        # Parse timestamp
        ts_str = obj.get("timestamp")
        if ts_str:
            try:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                timestamp = datetime.now()
        else:
            timestamp = datetime.now()

        # Extract content
        content = ""
        tool_calls = []
        thinking = None
        model = None
        token_usage = None

        if msg_type == "summary":
            content = obj.get("summary", "")
        else:
            message = obj.get("message", {})
            raw_content = message.get("content", "")

            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                text_parts = []
                for block in raw_content:
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_calls.append(
                                {
                                    "name": block.get("name"),
                                    "id": block.get("id"),
                                }
                            )
                        elif block.get("type") == "thinking":
                            thinking = block.get("thinking", "")
                content = "\n".join(text_parts)

            if msg_type == "assistant":
                model = message.get("model")
                usage = message.get("usage")
                if usage:
                    token_usage = {
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                    }

        return ConversationMessage(
            uuid=obj.get("uuid", ""),
            parent_uuid=obj.get("parentUuid"),
            type=msg_type,
            timestamp=timestamp,
            content=content,
            tool_calls=tool_calls,
            thinking=thinking,
            model=model,
            token_usage=token_usage,
        )

    def get_session_messages(
        self, session_id: str, limit: int = 100, offset: int = 0
    ) -> List[ConversationMessage]:
        """Get messages for a specific session."""
        session_file = self._find_session_file(session_id)
        if not session_file:
            return []

        messages = list(self._stream_messages(session_file))
        # Filter to only user and assistant messages
        messages = [m for m in messages if m.type in ("user", "assistant")]
        return messages[offset : offset + limit]

    def _find_session_file(self, session_id: str) -> Optional[Path]:
        """Find the session file for a given session ID."""
        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            session_file = project_dir / f"{session_id}.jsonl"
            if session_file.exists():
                return session_file
        return None

    def get_session(self, session_id: str) -> Optional[SessionMetadata]:
        """Get metadata for a specific session."""
        session_file = self._find_session_file(session_id)
        if not session_file:
            return None

        project_name = session_file.parent.name
        return self._parse_session_metadata(session_file, project_name)

    def get_session_todos(self, session_id: str) -> List[TodoItem]:
        """Get todos for a session."""
        # Try different naming patterns
        patterns = [
            f"{session_id}-agent-{session_id}.json",
            f"{session_id}.json",
        ]

        for pattern in patterns:
            todo_file = self.todos_dir / pattern
            if todo_file.exists():
                try:
                    with open(todo_file) as f:
                        todos_data = json.load(f)
                    if isinstance(todos_data, list):
                        return [
                            TodoItem(
                                content=t.get("content", ""),
                                status=t.get("status", "pending"),
                                active_form=t.get("activeForm"),
                            )
                            for t in todos_data
                        ]
                except Exception:
                    pass
        return []

    def get_stats(self) -> SessionStats:
        """Get aggregated statistics."""
        if not self.stats_file.exists():
            # Calculate from sessions if no cache
            sessions = self.get_all_sessions()
            return SessionStats(
                total_sessions=len(sessions),
                total_messages=sum(s.message_count for s in sessions),
            )

        try:
            with open(self.stats_file) as f:
                data = json.load(f)

            return SessionStats(
                total_sessions=data.get("totalSessions", 0),
                total_messages=data.get("totalMessages", 0),
                daily_activity=data.get("dailyActivity", []),
                model_usage=data.get("modelUsage", {}),
                longest_session=data.get("longestSession"),
                first_session_date=data.get("firstSessionDate"),
            )
        except Exception:
            return SessionStats()

    def search_sessions(
        self, query: str, search_content: bool = False
    ) -> List[SessionMetadata]:
        """Search sessions by summaries or content."""
        query_lower = query.lower()
        results = []

        for session in self.get_all_sessions():
            # Search in summaries
            if any(query_lower in s.lower() for s in session.summaries):
                results.append(session)
                continue

            # Search in project path
            if query_lower in session.project_path.lower():
                results.append(session)
                continue

            # Optionally search in message content
            if search_content and session.file_path:
                try:
                    with open(session.file_path) as f:
                        content = f.read().lower()
                    if query_lower in content:
                        results.append(session)
                except Exception:
                    pass

        return results
