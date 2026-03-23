import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Iterator, Dict, Any, Tuple

from ..config import settings
from .models import SessionMetadata, ConversationMessage, SessionStats, TodoItem, ToolCallDetail, ConversationThread, ConversationTree

# Patterns that indicate pasted/external content
PASTE_INDICATORS = [
    (r"skip to main content", "webpage"),
    (r"posted on\s+posted \d+", "job posting"),
    (r"job requisition id", "job posting"),
    (r"©\s*\d{4}", "webpage"),
    (r"terms of use.*privacy.*security", "webpage"),
    (r"log\s*out\s*\n.*home", "portal/dashboard"),
    (r"net\s*benefits|fidelity|vesting|rsus?|restricted.*(stock|unit)", "compensation data"),
    (r"annual.*(salary|bonus|base)", "compensation data"),
    (r"granted.*units|outstanding.*units", "stock/equity data"),
    (r"from:.*\nto:.*\nsubject:", "email"),
    (r"sent:.*\n.*subject:", "email"),
]


def _detect_pasted_content(text: str) -> List[str]:
    """Detect if a message contains pasted external content."""
    types = set()
    lower = text.lower()

    # Long messages (>500 chars) with structured content are likely pastes
    if len(text) > 1000:
        for pattern, content_type in PASTE_INDICATORS:
            if re.search(pattern, lower):
                types.add(content_type)

    # Very long messages are almost certainly pasted
    if len(text) > 3000 and not types:
        types.add("long text")

    return list(types)


def _generate_title(first_message: str) -> str:
    """Generate a readable title from the first user message."""
    # Clean up the message
    text = first_message.strip()

    # Handle Claude Code command messages like <command-message>standup</command-message>
    cmd_match = re.search(r"<command-name>/(\w+)</command-name>", text)
    if cmd_match:
        return f"/{cmd_match.group(1)}"

    # Remove XML-like tags
    text = re.sub(r"<[^>]+>", "", text).strip()

    # Remove pasted content — take just the user's instruction part
    # Usually the user's actual question is at the start before pasted content
    lines = text.split("\n")

    # Take first meaningful line(s) up to ~120 chars
    title_parts = []
    char_count = 0
    for line in lines:
        line = line.strip()
        if not line:
            if title_parts:
                break
            continue
        # Skip lines that look like pasted webpage content
        if any(
            indicator in line.lower()
            for indicator in ["skip to main", "sign in", "search for", "©", "posted on"]
        ):
            break
        title_parts.append(line)
        char_count += len(line)
        if char_count > 120:
            break

    title = " ".join(title_parts)

    # Truncate if still too long
    if len(title) > 80:
        title = title[:77] + "..."

    # If we couldn't extract a good title, use a truncated version
    if not title or len(title) < 3:
        title = text[:77] + "..." if len(text) > 80 else text

    return title


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

            # Generate title from first user message
            user_messages = [m for m in messages if m.type == "user"]
            first_user_msg = user_messages[0].content if user_messages else ""
            title = _generate_title(first_user_msg) if first_user_msg else None

            # Detect pasted content across all user messages
            all_paste_types: set = set()
            has_pasted = False
            for m in user_messages:
                ptypes = _detect_pasted_content(m.content)
                if ptypes:
                    has_pasted = True
                    all_paste_types.update(ptypes)

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
                title=title,
                first_user_message=first_user_msg[:500] if first_user_msg else None,
                has_pasted_content=has_pasted,
                pasted_content_types=list(all_paste_types),
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
        tool_details = []
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
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            tool_calls.append(
                                {
                                    "name": tool_name,
                                    "id": block.get("id"),
                                }
                            )
                            # Extract rich tool details
                            detail = self._extract_tool_detail(
                                tool_name, tool_input, block.get("id")
                            )
                            tool_details.append(detail)
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
            tool_details=tool_details,
            thinking=thinking,
            model=model,
            token_usage=token_usage,
            is_sidechain=obj.get("isSidechain", False),
        )

    def _extract_tool_detail(
        self, name: str, input_data: Any, tool_id: Optional[str] = None
    ) -> ToolCallDetail:
        """Extract a readable summary from a tool_use block."""
        if not isinstance(input_data, dict):
            return ToolCallDetail(name=name, id=tool_id)

        file_path = input_data.get("file_path") or input_data.get("path")
        command = input_data.get("command")
        query = input_data.get("query") or input_data.get("pattern")

        # Build a short summary based on tool type
        summary = None
        if name == "Read":
            summary = f"Read {file_path}" if file_path else None
        elif name == "Write":
            summary = f"Write {file_path}" if file_path else None
        elif name == "Edit":
            old = input_data.get("old_string", "")
            summary = f"Edit {file_path}" if file_path else None
            if old:
                summary += f" (replacing {len(old)} chars)"
        elif name == "Bash":
            desc = input_data.get("description", "")
            summary = desc or (command[:80] + "..." if command and len(command) > 80 else command)
        elif name == "Grep":
            summary = f"Search for '{query}'" if query else None
        elif name == "Glob":
            summary = f"Find files: {query}" if query else None
        elif name == "Agent":
            desc = input_data.get("description", "")
            summary = f"Agent: {desc}" if desc else None
        elif name == "WebSearch":
            summary = f"Search: {query}" if query else None
        elif name == "WebFetch":
            url = input_data.get("url", "")
            summary = f"Fetch: {url[:60]}" if url else None
        elif name.startswith("mcp__"):
            # Generic MCP tool: extract the last segment as a readable name
            parts = name.split("__")
            service = parts[2] if len(parts) > 2 else parts[-1]
            action = parts[-1] if len(parts) > 3 else ""
            summary = f"{service}: {action}" if action and action != service else service
        else:
            # Generic: try to build something useful
            summary = name

        return ToolCallDetail(
            name=name,
            id=tool_id,
            input_summary=summary,
            file_path=file_path,
            command=command,
            query=query,
        )

    def get_session_messages(
        self, session_id: str, limit: int = 500, offset: int = 0
    ) -> List[ConversationMessage]:
        """Get messages for a specific session."""
        session_file = self._find_session_file(session_id)
        if not session_file:
            return []

        messages = list(self._stream_messages(session_file))
        # Filter to only user and assistant messages
        messages = [m for m in messages if m.type in ("user", "assistant")]
        return messages[offset : offset + limit]

    def get_conversation_tree(self, session_id: str) -> ConversationTree:
        """Build a tree-structured view of the conversation.

        Groups messages into threads (user question + Claude responses),
        detects branches where the conversation forked, and builds
        a collapsible tree structure.
        """
        session_file = self._find_session_file(session_id)
        if not session_file:
            return ConversationTree()

        # Parse all messages with full metadata
        all_msgs = []
        with open(session_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    msg = self._parse_message(obj)
                    if msg and msg.type in ("user", "assistant"):
                        all_msgs.append(msg)
                except (json.JSONDecodeError, Exception):
                    continue

        if not all_msgs:
            return ConversationTree()

        # Build a lookup: uuid -> message, parent -> children
        by_uuid: Dict[str, ConversationMessage] = {}
        children: Dict[Optional[str], List[str]] = {}
        for m in all_msgs:
            by_uuid[m.uuid] = m
            parent = m.parent_uuid
            children.setdefault(parent, []).append(m.uuid)

        # Find branch points
        branch_points = {k for k, v in children.items() if len(v) > 1 and k is not None}

        # Walk the main chain (non-sidechain path) and group into threads
        # A "thread" = one user message + all consecutive assistant messages
        def walk_chain(start_uuid: Optional[str], depth: int = 0) -> List[ConversationThread]:
            threads = []
            current_thread = None
            visited = set()

            # Get children of start point
            child_uuids = children.get(start_uuid, [])
            queue = list(child_uuids)

            while queue:
                uid = queue.pop(0)
                if uid in visited:
                    continue
                visited.add(uid)

                msg = by_uuid.get(uid)
                if not msg:
                    continue

                if msg.type == "user":
                    # Start a new thread
                    if current_thread:
                        threads.append(current_thread)
                    current_thread = ConversationThread(
                        thread_id=uid,
                        user_message=msg,
                        depth=depth,
                        is_sidechain=msg.is_sidechain,
                    )
                elif msg.type == "assistant":
                    if current_thread is None:
                        # Assistant message without a user message (first msg)
                        current_thread = ConversationThread(
                            thread_id=uid,
                            depth=depth,
                        )
                    current_thread.assistant_messages.append(msg)

                # Check if this node is a branch point
                msg_children = children.get(uid, [])
                if len(msg_children) > 1:
                    # Main chain continues with first non-sidechain child
                    main_child = None
                    side_children = []
                    for c in msg_children:
                        c_msg = by_uuid.get(c)
                        if c_msg and c_msg.is_sidechain:
                            side_children.append(c)
                        elif main_child is None:
                            main_child = c
                        else:
                            side_children.append(c)

                    # Build branch threads for sidechains
                    if current_thread:
                        for sc in side_children:
                            branch_threads = walk_chain(uid, depth + 1)
                            # Filter to only the sidechain branch
                            sc_msg = by_uuid.get(sc)
                            if sc_msg:
                                branch_thread = ConversationThread(
                                    thread_id=sc,
                                    user_message=sc_msg if sc_msg.type == "user" else None,
                                    depth=depth + 1,
                                    is_sidechain=True,
                                )
                                if sc_msg.type == "assistant":
                                    branch_thread.assistant_messages.append(sc_msg)
                                current_thread.branch_children.append(branch_thread)

                    if main_child:
                        queue.insert(0, main_child)
                elif len(msg_children) == 1:
                    queue.insert(0, msg_children[0])

            if current_thread:
                threads.append(current_thread)

            return threads

        # Start from root (messages with no parent)
        root_children = children.get(None, [])
        threads = []
        current_thread = None

        # Simple linear grouping for the main view
        for msg in all_msgs:
            if msg.is_sidechain:
                continue
            if msg.type == "user":
                if current_thread:
                    threads.append(current_thread)
                current_thread = ConversationThread(
                    thread_id=msg.uuid,
                    user_message=msg,
                )
            elif msg.type == "assistant":
                if current_thread is None:
                    current_thread = ConversationThread(thread_id=msg.uuid)
                current_thread.assistant_messages.append(msg)

        if current_thread:
            threads.append(current_thread)

        return ConversationTree(
            threads=threads,
            branch_points=len(branch_points),
            total_messages=len(all_msgs),
        )

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
