"""Permanent conversation archive.

Stores full conversation content in SQLite so sessions persist
after Claude Code deletes the JSONL files (~30 days).
"""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..config import settings
from .session_parser import SessionParser, _generate_title, _detect_pasted_content


DB_PATH = settings.claude_data_dir / "session-archive.db"


class SessionArchive:
    """Permanent archive of Claude Code conversations."""

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.parser = SessionParser()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS archived_sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT,
                    project_path TEXT,
                    start_time TEXT,
                    last_activity TEXT,
                    message_count INTEGER,
                    user_message_count INTEGER,
                    assistant_message_count INTEGER,
                    model_used TEXT,
                    total_input_tokens INTEGER DEFAULT 0,
                    total_output_tokens INTEGER DEFAULT 0,
                    has_pasted_content INTEGER DEFAULT 0,
                    pasted_content_types TEXT DEFAULT '[]',
                    archived_at REAL,
                    source_file TEXT,
                    is_live INTEGER DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS archived_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    uuid TEXT,
                    parent_uuid TEXT,
                    type TEXT,
                    timestamp TEXT,
                    content TEXT,
                    tool_details_json TEXT DEFAULT '[]',
                    thinking TEXT,
                    model TEXT,
                    is_sidechain INTEGER DEFAULT 0,
                    sort_order INTEGER,
                    FOREIGN KEY (session_id) REFERENCES archived_sessions(session_id)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON archived_messages(session_id, sort_order);

                CREATE TABLE IF NOT EXISTS archive_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                -- FTS for searching archived content
                CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
                    session_id UNINDEXED,
                    title,
                    content,
                    tokenize='porter'
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def archive_session(self, session_id: str, force: bool = False) -> bool:
        """Archive a single session. Returns True if archived, False if already exists."""
        conn = self._connect()
        try:
            # Check if already archived with same message count
            if not force:
                existing = conn.execute(
                    "SELECT message_count, is_live FROM archived_sessions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                if existing:
                    # Update is_live status but don't re-archive if message count matches
                    session_file = self._find_session_file(session_id)
                    is_live = 1 if session_file else 0
                    if existing["is_live"] != is_live:
                        conn.execute(
                            "UPDATE archived_sessions SET is_live = ? WHERE session_id = ?",
                            (is_live, session_id),
                        )
                        conn.commit()
                    return False

            # Parse the session
            session = self.parser.get_session(session_id)
            if not session:
                return False

            messages = self.parser.get_session_messages(session_id, limit=5000)
            if not messages:
                return False

            session_file = self._find_session_file(session_id)

            # Delete old data if re-archiving
            conn.execute("DELETE FROM archived_messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM archived_sessions WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM archive_fts WHERE session_id = ?", (session_id,))

            # Insert session metadata
            conn.execute(
                """INSERT INTO archived_sessions
                   (session_id, title, project_path, start_time, last_activity,
                    message_count, user_message_count, assistant_message_count,
                    model_used, total_input_tokens, total_output_tokens,
                    has_pasted_content, pasted_content_types,
                    archived_at, source_file, is_live)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    session.title,
                    session.project_path,
                    session.start_time.isoformat(),
                    session.last_activity.isoformat(),
                    session.message_count,
                    session.user_message_count,
                    session.assistant_message_count,
                    session.model_used,
                    session.total_input_tokens,
                    session.total_output_tokens,
                    1 if session.has_pasted_content else 0,
                    json.dumps(session.pasted_content_types),
                    time.time(),
                    str(session_file) if session_file else None,
                    1 if session_file else 0,
                ),
            )

            # Insert all messages
            all_content_parts = []
            for i, msg in enumerate(messages):
                tool_details = [
                    {"name": td.name, "input_summary": td.input_summary,
                     "file_path": td.file_path, "command": td.command}
                    for td in msg.tool_details
                ]
                conn.execute(
                    """INSERT INTO archived_messages
                       (session_id, uuid, parent_uuid, type, timestamp, content,
                        tool_details_json, thinking, model, is_sidechain, sort_order)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        session_id,
                        msg.uuid,
                        msg.parent_uuid,
                        msg.type,
                        msg.timestamp.isoformat(),
                        msg.content,
                        json.dumps(tool_details),
                        msg.thinking,
                        msg.model,
                        1 if msg.is_sidechain else 0,
                        i,
                    ),
                )
                if msg.content:
                    all_content_parts.append(msg.content)

            # Index for FTS
            all_content = "\n".join(all_content_parts)
            conn.execute(
                "INSERT INTO archive_fts (session_id, title, content) VALUES (?, ?, ?)",
                (session_id, session.title or "", all_content),
            )

            conn.commit()
            return True
        except Exception as e:
            print(f"Archive error for {session_id}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def archive_all(self) -> Dict[str, int]:
        """Archive all current sessions. Returns counts."""
        sessions = self.parser.get_all_sessions()
        archived = 0
        updated = 0
        for session in sessions:
            result = self.archive_session(session.session_id)
            if result:
                archived += 1
            else:
                updated += 1

        # Mark sessions whose JSONL files no longer exist
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, source_file FROM archived_sessions WHERE is_live = 1"
            ).fetchall()
            gone = 0
            for row in rows:
                if row["source_file"] and not Path(row["source_file"]).exists():
                    conn.execute(
                        "UPDATE archived_sessions SET is_live = 0 WHERE session_id = ?",
                        (row["session_id"],),
                    )
                    gone += 1
            conn.commit()
        finally:
            conn.close()

        # Update last archive time
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO archive_meta (key, value) VALUES (?, ?)",
                ("last_archive_time", str(time.time())),
            )
            conn.commit()
        finally:
            conn.close()

        return {"new": archived, "existing": updated, "gone": gone}

    def get_archived_session(self, session_id: str) -> Optional[dict]:
        """Get archived session metadata."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM archived_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return dict(row)
            return None
        finally:
            conn.close()

    def get_archived_messages(self, session_id: str) -> List[dict]:
        """Get archived messages for a session."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM archived_messages
                   WHERE session_id = ?
                   ORDER BY sort_order""",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_all_archived(self, limit: int = 200) -> List[dict]:
        """Get all archived sessions, sorted by last activity."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM archived_sessions
                   ORDER BY last_activity DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_archived_only(self, limit: int = 100) -> List[dict]:
        """Get sessions that exist ONLY in archive (JSONL deleted)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM archived_sessions
                   WHERE is_live = 0
                   ORDER BY last_activity DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def search(self, query: str, limit: int = 50) -> List[dict]:
        """Full-text search across archived conversations."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT
                    a.session_id, a.title, a.project_path,
                    a.last_activity, a.message_count, a.is_live,
                    snippet(archive_fts, 2, '>>>', '<<<', '...', 48) AS snippet
                FROM archive_fts
                JOIN archived_sessions a ON a.session_id = archive_fts.session_id
                WHERE archive_fts MATCH ?
                ORDER BY rank
                LIMIT ?""",
                (query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            # Fallback for syntax errors
            escaped = '"' + query.replace('"', '""') + '"'
            try:
                return self.search(escaped, limit)
            except Exception:
                return []
        finally:
            conn.close()

    def search_messages(self, query: str, limit: int = 50) -> list:
        """Message-level search in archived conversations.
        Returns individual messages with UUIDs for deep-linking."""
        import re
        conn = self._connect()
        try:
            query_words = set(re.findall(r"[a-zA-Z]{3,}", query.lower()))
            if not query_words:
                return []

            # Search archive_fts first to find matching sessions
            try:
                session_rows = conn.execute(
                    """SELECT a.session_id, a.title
                    FROM archive_fts
                    JOIN archived_sessions a ON a.session_id = archive_fts.session_id
                    WHERE archive_fts MATCH ?
                    ORDER BY rank LIMIT 30""",
                    (query,),
                ).fetchall()
            except Exception:
                escaped = '"' + query.replace('"', '""') + '"'
                session_rows = conn.execute(
                    """SELECT a.session_id, a.title
                    FROM archive_fts
                    JOIN archived_sessions a ON a.session_id = archive_fts.session_id
                    WHERE archive_fts MATCH ?
                    ORDER BY rank LIMIT 30""",
                    (escaped,),
                ).fetchall()

            results = []
            for srow in session_rows:
                sid = srow["session_id"]
                title = srow["title"]
                messages = conn.execute(
                    "SELECT uuid, type, timestamp, content FROM archived_messages WHERE session_id = ? ORDER BY sort_order",
                    (sid,),
                ).fetchall()
                for msg in messages:
                    content = msg["content"] or ""
                    if not content or msg["type"] not in ("user", "assistant"):
                        continue
                    content_lower = content.lower()
                    match_count = sum(1 for w in query_words if w in content_lower)
                    if match_count >= max(1, len(query_words) // 2):
                        # Build snippet
                        best_pos = len(content)
                        for word in query_words:
                            pos = content_lower.find(word)
                            if pos != -1 and pos < best_pos:
                                best_pos = pos
                        if best_pos == len(content):
                            best_pos = 0
                        start = max(0, best_pos - 60)
                        end = min(len(content), best_pos + 120)
                        snippet = content[start:end].strip()
                        if start > 0:
                            snippet = "..." + snippet
                        if end < len(content):
                            snippet = snippet + "..."
                        for word in query_words:
                            pattern = re.compile(re.escape(word), re.IGNORECASE)
                            snippet = pattern.sub(lambda m: f">>>{m.group(0)}<<<", snippet)

                        results.append({
                            "session_id": sid,
                            "session_title": title,
                            "message_uuid": msg["uuid"] or "",
                            "message_type": msg["type"],
                            "timestamp": msg["timestamp"],
                            "snippet": snippet,
                            "match_score": match_count / len(query_words) if query_words else 0,
                        })

            results.sort(key=lambda r: -r["match_score"])
            return results[:limit]
        finally:
            conn.close()

    def _mark_gone_sessions(self) -> int:
        """Mark sessions whose JSONL files no longer exist."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT session_id, source_file FROM archived_sessions WHERE is_live = 1"
            ).fetchall()
            gone = 0
            for row in rows:
                if row["source_file"] and not Path(row["source_file"]).exists():
                    conn.execute(
                        "UPDATE archived_sessions SET is_live = 0 WHERE session_id = ?",
                        (row["session_id"],),
                    )
                    gone += 1
            conn.commit()
            return gone
        finally:
            conn.close()

    def get_last_archive_time(self) -> Optional[float]:
        """Get the timestamp of the last archive run."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM archive_meta WHERE key = 'last_archive_time'"
            ).fetchone()
            return float(row["value"]) if row else None
        except Exception:
            return None
        finally:
            conn.close()

    def get_stats(self) -> dict:
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) as c FROM archived_sessions").fetchone()["c"]
            live = conn.execute("SELECT COUNT(*) as c FROM archived_sessions WHERE is_live = 1").fetchone()["c"]
            archived = conn.execute("SELECT COUNT(*) as c FROM archived_sessions WHERE is_live = 0").fetchone()["c"]
            msgs = conn.execute("SELECT COUNT(*) as c FROM archived_messages").fetchone()["c"]
            return {"total": total, "live": live, "archived_only": archived, "total_messages": msgs}
        finally:
            conn.close()

    def _find_session_file(self, session_id: str) -> Optional[Path]:
        projects_dir = settings.claude_data_dir / "projects"
        if not projects_dir.exists():
            return None
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            f = project_dir / f"{session_id}.jsonl"
            if f.exists():
                return f
        return None
