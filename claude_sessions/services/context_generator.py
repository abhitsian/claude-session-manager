from datetime import datetime
from typing import List, Optional

from ..data.models import SessionContext, ConversationMessage, TodoItem
from ..data.session_parser import SessionParser
from ..data.active_detector import ActiveSessionDetector


class ContextGenerator:
    """Generate context summaries for session continuation."""

    def __init__(self, parser: SessionParser, detector: ActiveSessionDetector):
        self.parser = parser
        self.detector = detector

    def generate_context(
        self,
        session_id: str,
        include_files: bool = True,
        include_todos: bool = True,
        max_recent_messages: int = 10,
    ) -> SessionContext:
        """Generate a context summary for continuing a session."""
        session = self.parser.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        # Get recent messages
        all_messages = self.parser.get_session_messages(session_id, limit=1000)
        recent_messages = all_messages[-max_recent_messages:] if all_messages else []

        # Get todos
        todos = self.parser.get_session_todos(session_id) if include_todos else []
        pending_todos = [t for t in todos if t.status != "completed"]

        # Extract file references from tool calls
        key_files: List[str] = []
        if include_files:
            key_files = self._extract_file_references(all_messages)

        # Calculate duration
        duration_minutes = int(
            (session.last_activity - session.start_time).total_seconds() / 60
        )

        # Build summary from session summaries or recent context
        summary = self._build_summary(session, recent_messages)

        # Generate continuation prompt
        continuation_prompt = self._generate_continuation_prompt(
            session, summary, key_files, pending_todos, recent_messages
        )

        # Generate resume command (if Claude Code supports it)
        resume_command = f"claude --continue {session_id}"

        return SessionContext(
            session_id=session_id,
            project_path=session.project_path,
            start_time=session.start_time,
            last_activity=session.last_activity,
            duration_minutes=duration_minutes,
            summary=summary,
            key_files=key_files[:20],  # Limit to 20 files
            pending_todos=pending_todos,
            recent_messages=recent_messages,
            continuation_prompt=continuation_prompt,
            resume_command=resume_command,
        )

    def _extract_file_references(
        self, messages: List[ConversationMessage]
    ) -> List[str]:
        """Extract unique file paths from tool calls."""
        files = set()
        for msg in messages:
            for tool in msg.tool_calls:
                name = tool.get("name", "")
                if name in ("Read", "Write", "Edit", "Glob"):
                    # We don't have the full tool input here, but we could
                    # parse it from the raw message if needed
                    pass
        return list(files)

    def _build_summary(
        self, session, messages: List[ConversationMessage]
    ) -> str:
        """Build a summary of the session."""
        # Use stored summaries if available
        if session.summaries:
            return "\n".join(session.summaries[:3])

        # Otherwise summarize from recent user messages
        user_messages = [m for m in messages if m.type == "user"][-5:]
        if user_messages:
            topics = [m.content[:200] for m in user_messages]
            return "Recent topics discussed:\n- " + "\n- ".join(topics)

        return "No summary available"

    def _generate_continuation_prompt(
        self,
        session,
        summary: str,
        key_files: List[str],
        pending_todos: List[TodoItem],
        recent_messages: List[ConversationMessage],
    ) -> str:
        """Generate a markdown prompt for continuing the session."""
        lines = [
            "# Session Context Continuation",
            "",
            "## Original Session",
            f"- **Session ID**: `{session.session_id}`",
            f"- **Project**: `{session.project_path}`",
            f"- **Started**: {session.start_time.strftime('%Y-%m-%d %H:%M')}",
            f"- **Last Activity**: {session.last_activity.strftime('%Y-%m-%d %H:%M')}",
            f"- **Messages**: {session.message_count} ({session.user_message_count} user, {session.assistant_message_count} assistant)",
            "",
        ]

        if session.model_used:
            lines.append(f"- **Model**: {session.model_used}")
            lines.append("")

        # Summary section
        lines.extend(["## Session Summary", summary, ""])

        # Key files
        if key_files:
            lines.append("## Key Files")
            for f in key_files[:10]:
                lines.append(f"- `{f}`")
            lines.append("")

        # Pending todos
        if pending_todos:
            lines.append("## Pending Tasks")
            for todo in pending_todos:
                status_icon = "[ ]" if todo.status == "pending" else "[~]"
                lines.append(f"- {status_icon} {todo.content}")
            lines.append("")

        # Recent conversation
        if recent_messages:
            lines.append("## Recent Conversation")
            for msg in recent_messages[-5:]:
                role = "User" if msg.type == "user" else "Assistant"
                content = msg.content[:500]
                if len(msg.content) > 500:
                    content += "..."
                lines.append(f"\n**{role}** ({msg.timestamp.strftime('%H:%M')}):")
                lines.append(content)
            lines.append("")

        # Continuation instructions
        lines.extend(
            [
                "## Continue From Here",
                "Please continue working on this session. Review the context above and pick up where we left off.",
                "",
            ]
        )

        return "\n".join(lines)
