"""LLM-powered session description generator.

Uses ollama (local) to generate concise narrative descriptions of sessions
covering the high-level topics discussed back and forth. Falls back to
heuristic summaries if ollama is unavailable.

Descriptions are cached in SQLite so each session is only described once.
"""

import json
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, List, Dict

from ..config import settings
from ..data.session_parser import SessionParser

DB_PATH = Path.home() / ".claude" / "session-descriptions.db"

# Ollama settings
OLLAMA_URL = "http://localhost:11434/api/generate"
# Prefer smaller/faster models first, fall back to larger ones
OLLAMA_MODELS = ["qwen2:7b", "qwen2:1.5b", "phi3:latest", "llama2:latest"]


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_descriptions (
            session_id TEXT PRIMARY KEY,
            description TEXT,
            model_used TEXT,
            generated_at REAL,
            message_count INTEGER
        )
    """)
    conn.commit()
    return conn


def _build_conversation_digest(session_id: str, parser: SessionParser, max_turns: int = 30) -> str:
    """Build a compact digest of the conversation for the LLM.

    Extracts user messages and short assistant summaries to capture
    the back-and-forth flow without exceeding context limits.
    """
    messages = parser.get_session_messages(session_id, limit=2000)
    if not messages:
        return ""

    lines = []
    turn_count = 0

    for msg in messages:
        if turn_count >= max_turns:
            break

        if msg.type == "user" and msg.content.strip():
            # User messages: include first 300 chars
            content = msg.content.strip()
            # Strip XML/system tags
            import re
            content = re.sub(r"<[^>]+>", "", content)
            content = re.sub(r"\s+", " ", content).strip()
            if len(content) < 10:
                continue
            if len(content) > 300:
                content = content[:297] + "..."
            lines.append(f"User: {content}")
            turn_count += 1

        elif msg.type == "assistant" and msg.content.strip():
            # Assistant: just first 200 chars to show response direction
            content = msg.content.strip()
            import re
            content = re.sub(r"<[^>]+>", "", content)
            content = re.sub(r"\s+", " ", content).strip()
            if len(content) < 10:
                continue
            if len(content) > 200:
                content = content[:197] + "..."
            lines.append(f"Claude: {content}")
            turn_count += 1

    return "\n".join(lines)


def _call_ollama(prompt: str, model: Optional[str] = None) -> Optional[str]:
    """Call ollama API to generate text. Returns None if ollama is unavailable."""
    if model is None:
        # Try models in order of preference
        for m in OLLAMA_MODELS:
            result = _call_ollama(prompt, model=m)
            if result is not None:
                return result
        return None

    try:
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 200,
            },
        }).encode("utf-8")

        req = urllib.request.Request(
            OLLAMA_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get("response", "").strip()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, OSError):
        return None


def _generate_heuristic_description(session_id: str, parser: SessionParser) -> str:
    """Fallback: build a description from heuristics when ollama is unavailable."""
    messages = parser.get_session_messages(session_id, limit=500)
    if not messages:
        return "Empty session."

    import re

    user_msgs = [m for m in messages if m.type == "user" and m.content.strip()]
    if not user_msgs:
        return "Session with no user messages."

    topics = []
    for msg in user_msgs[:15]:
        content = re.sub(r"<[^>]+>", "", msg.content)
        content = re.sub(r"```[\s\S]*?```", "[code]", content)
        content = re.sub(r"\s+", " ", content).strip()
        first_line = re.split(r"[.!?\n]", content)[0].strip()
        if len(first_line) > 10 and first_line[:30].lower() not in {t[:30].lower() for t in topics}:
            if len(first_line) > 80:
                first_line = first_line[:77] + "..."
            topics.append(first_line)

    if not topics:
        return "Brief session."

    if len(topics) == 1:
        return f"Discussed: {topics[0]}."

    parts = [topics[0]]
    for t in topics[1:4]:
        parts.append(t)

    if len(topics) > 4:
        return f"Started with {parts[0].lower()}, then covered {', '.join(p.lower() for p in parts[1:])}, and {len(topics) - 4} more topics."

    return f"Discussed {', '.join(p.lower() for p in parts[:-1])}, and {parts[-1].lower()}."


def describe_session(session_id: str, parser: Optional[SessionParser] = None, force: bool = False) -> str:
    """Generate or retrieve a cached description for a session.

    Returns a 2-4 sentence narrative description of the session's
    high-level topics and flow.
    """
    if parser is None:
        parser = SessionParser()

    conn = _get_db()
    try:
        # Check cache
        if not force:
            row = conn.execute(
                "SELECT description, message_count FROM session_descriptions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                # Re-generate if session has grown significantly
                session = parser.get_session(session_id)
                if session and session.message_count > (row["message_count"] or 0) + 10:
                    pass  # Fall through to regenerate
                else:
                    return row["description"]

        # Build digest
        digest = _build_conversation_digest(session_id, parser)
        if not digest:
            return "Empty session."

        # Try ollama first
        prompt = (
            "Summarize this Claude Code conversation in 2-3 sentences. "
            "Describe the main topics discussed back and forth between the user and Claude. "
            "Be specific about what was discussed, not generic. Use past tense. "
            "Do not start with 'In this session' or 'The user'. Just describe what happened.\n\n"
            f"{digest}\n\n"
            "Summary:"
        )

        description = _call_ollama(prompt)
        model_used = "ollama"

        if not description:
            # Fallback to heuristic
            description = _generate_heuristic_description(session_id, parser)
            model_used = "heuristic"

        # Clean up LLM output
        description = description.strip().strip('"').strip()
        # Remove any markdown formatting the LLM might add
        if description.startswith("**") and description.endswith("**"):
            description = description[2:-2]
        # Cap length
        if len(description) > 500:
            description = description[:497] + "..."

        # Get message count for staleness tracking
        session = parser.get_session(session_id)
        msg_count = session.message_count if session else 0

        # Cache
        conn.execute(
            """INSERT OR REPLACE INTO session_descriptions
               (session_id, description, model_used, generated_at, message_count)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, description, model_used, time.time(), msg_count),
        )
        conn.commit()

        return description
    finally:
        conn.close()


def describe_sessions_batch(
    session_ids: List[str], parser: Optional[SessionParser] = None
) -> Dict[str, str]:
    """Get descriptions for multiple sessions, using cache where available.

    Returns dict of session_id -> description.
    """
    if parser is None:
        parser = SessionParser()

    conn = _get_db()
    results = {}

    try:
        # Load all cached descriptions
        placeholders = ",".join("?" for _ in session_ids)
        rows = conn.execute(
            f"SELECT session_id, description FROM session_descriptions WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
        cached = {row["session_id"]: row["description"] for row in rows}
    finally:
        conn.close()

    # Return cached ones and generate missing ones
    for sid in session_ids:
        if sid in cached:
            results[sid] = cached[sid]
        else:
            results[sid] = describe_session(sid, parser)

    return results


def get_cached_description(session_id: str) -> Optional[str]:
    """Get a cached description without generating one. Returns None if not cached."""
    conn = _get_db()
    try:
        row = conn.execute(
            "SELECT description FROM session_descriptions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row["description"] if row else None
    finally:
        conn.close()
