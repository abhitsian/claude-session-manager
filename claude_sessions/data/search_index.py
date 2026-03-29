"""SQLite FTS5-based search index for Claude Code sessions."""

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from ..config import settings
from .session_parser import SessionParser, _generate_title


DB_PATH = Path.home() / ".claude" / "session-search.db"


class SearchIndex:
    """Full-text search index for Claude Code sessions using SQLite FTS5."""

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
        """Create tables if they don't exist."""
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    title TEXT,
                    first_user_message TEXT,
                    all_content TEXT,
                    project_path TEXT,
                    start_time TEXT,
                    last_activity TEXT,
                    message_count INTEGER,
                    file_path TEXT,
                    file_mtime REAL
                );

                CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
                    session_id UNINDEXED,
                    title,
                    first_user_message,
                    all_content,
                    project_path,
                    content='sessions',
                    content_rowid='rowid'
                );

                -- Triggers to keep FTS in sync with content table
                CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
                    INSERT INTO sessions_fts(rowid, session_id, title, first_user_message, all_content, project_path)
                    VALUES (new.rowid, new.session_id, new.title, new.first_user_message, new.all_content, new.project_path);
                END;

                CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
                    INSERT INTO sessions_fts(sessions_fts, rowid, session_id, title, first_user_message, all_content, project_path)
                    VALUES ('delete', old.rowid, old.session_id, old.title, old.first_user_message, old.all_content, old.project_path);
                END;

                CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
                    INSERT INTO sessions_fts(sessions_fts, rowid, session_id, title, first_user_message, all_content, project_path)
                    VALUES ('delete', old.rowid, old.session_id, old.title, old.first_user_message, old.all_content, old.project_path);
                    INSERT INTO sessions_fts(rowid, session_id, title, first_user_message, all_content, project_path)
                    VALUES (new.rowid, new.session_id, new.title, new.first_user_message, new.all_content, new.project_path);
                END;

                CREATE TABLE IF NOT EXISTS index_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );

                CREATE TABLE IF NOT EXISTS session_summaries (
                    session_id TEXT PRIMARY KEY,
                    summary_json TEXT,
                    generated_at REAL
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_session_files(self) -> List[Path]:
        """Get all JSONL session files from the projects directory."""
        projects_dir = settings.claude_data_dir / "projects"
        if not projects_dir.exists():
            return []

        files = []
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for f in project_dir.glob("*.jsonl"):
                if not f.stem.startswith("agent-"):
                    files.append(f)
        return files

    def _parse_session_for_index(self, file_path: Path) -> Optional[Dict[str, Any]]:
        """Parse a single session file and extract fields for indexing."""
        try:
            messages = list(self.parser._stream_messages(file_path))
            if not messages:
                return None

            timestamps = [m.timestamp for m in messages]
            start_time = min(timestamps)
            last_activity = max(timestamps)

            user_messages = [m for m in messages if m.type == "user"]
            assistant_messages = [m for m in messages if m.type == "assistant"]

            first_user_msg = user_messages[0].content if user_messages else ""
            title = _generate_title(first_user_msg) if first_user_msg else ""

            # Concatenate all user + assistant text for full-text search
            all_parts = []
            for m in messages:
                if m.type in ("user", "assistant") and m.content:
                    all_parts.append(m.content)
            all_content = "\n".join(all_parts)

            # Decode project path from directory name
            project_name = file_path.parent.name
            project_path = project_name.replace("-", "/")
            if not project_path.startswith("/"):
                project_path = "/" + project_path

            return {
                "session_id": file_path.stem,
                "title": title,
                "first_user_message": first_user_msg[:1000] if first_user_msg else "",
                "all_content": all_content,
                "project_path": project_path,
                "start_time": start_time.isoformat(),
                "last_activity": last_activity.isoformat(),
                "message_count": len(messages),
                "file_path": str(file_path),
                "file_mtime": file_path.stat().st_mtime,
            }
        except Exception as e:
            print(f"SearchIndex: error parsing {file_path}: {e}")
            return None

    def build_index(self) -> int:
        """Full rebuild of the search index. Returns number of sessions indexed."""
        conn = self._connect()
        try:
            # Drop and recreate FTS triggers/tables for clean rebuild
            conn.executescript("""
                DELETE FROM sessions;
                INSERT INTO sessions_fts(sessions_fts) VALUES('delete-all');
            """)

            session_files = self._get_session_files()
            count = 0

            for file_path in session_files:
                data = self._parse_session_for_index(file_path)
                if data:
                    conn.execute(
                        """INSERT OR REPLACE INTO sessions
                           (session_id, title, first_user_message, all_content,
                            project_path, start_time, last_activity, message_count,
                            file_path, file_mtime)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            data["session_id"],
                            data["title"],
                            data["first_user_message"],
                            data["all_content"],
                            data["project_path"],
                            data["start_time"],
                            data["last_activity"],
                            data["message_count"],
                            data["file_path"],
                            data["file_mtime"],
                        ),
                    )
                    count += 1

            # Record last index time
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("last_index_time", str(time.time())),
            )
            conn.commit()
            return count
        finally:
            conn.close()

    def update_index(self) -> int:
        """Incremental update: only re-index sessions modified since last run.
        Returns number of sessions updated."""
        conn = self._connect()
        try:
            # Get last index time
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'last_index_time'"
            ).fetchone()
            last_index_time = float(row["value"]) if row else 0.0

            session_files = self._get_session_files()
            count = 0

            for file_path in session_files:
                mtime = file_path.stat().st_mtime
                if mtime <= last_index_time:
                    continue

                data = self._parse_session_for_index(file_path)
                if data:
                    # Delete old row if exists (triggers handle FTS cleanup)
                    conn.execute(
                        "DELETE FROM sessions WHERE session_id = ?",
                        (data["session_id"],),
                    )
                    conn.execute(
                        """INSERT INTO sessions
                           (session_id, title, first_user_message, all_content,
                            project_path, start_time, last_activity, message_count,
                            file_path, file_mtime)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            data["session_id"],
                            data["title"],
                            data["first_user_message"],
                            data["all_content"],
                            data["project_path"],
                            data["start_time"],
                            data["last_activity"],
                            data["message_count"],
                            data["file_path"],
                            data["file_mtime"],
                        ),
                    )
                    count += 1

            # Update last index time
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
                ("last_index_time", str(time.time())),
            )
            conn.commit()
            return count
        finally:
            conn.close()

    def search(self, query: str, limit: int = 50) -> List[dict]:
        """Full-text search across indexed sessions.

        Returns list of dicts with: session_id, title, snippet, score,
        project_path, last_activity, message_count.
        """
        conn = self._connect()
        try:
            # Use FTS5 match with BM25 ranking
            rows = conn.execute(
                """
                SELECT
                    s.session_id,
                    s.title,
                    snippet(sessions_fts, 3, '>>>', '<<<', '...', 48) AS snippet,
                    bm25(sessions_fts, 0, 5.0, 3.0, 1.0, 2.0) AS score,
                    s.project_path,
                    s.last_activity,
                    s.message_count
                FROM sessions_fts
                JOIN sessions s ON s.rowid = sessions_fts.rowid
                WHERE sessions_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (query, limit),
            ).fetchall()

            return [
                {
                    "session_id": row["session_id"],
                    "title": row["title"],
                    "snippet": row["snippet"],
                    "score": row["score"],
                    "project_path": row["project_path"],
                    "last_activity": row["last_activity"],
                    "message_count": row["message_count"],
                }
                for row in rows
            ]
        except Exception as e:
            # If query syntax is invalid, fall back to simple prefix match
            if "fts5" in str(e).lower() or "syntax" in str(e).lower():
                escaped = '"' + query.replace('"', '""') + '"'
                return self.search(escaped, limit)
            raise
        finally:
            conn.close()

    def get_summary(self, session_id: str) -> Optional[dict]:
        """Get cached summary for a session, or None if not cached."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT summary_json FROM session_summaries WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row:
                return json.loads(row["summary_json"])
            return None
        except Exception:
            return None
        finally:
            conn.close()

    def save_summary(self, session_id: str, summary: dict) -> None:
        """Cache a summary for a session."""
        conn = self._connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO session_summaries (session_id, summary_json, generated_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(summary), time.time()),
            )
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()

    def search_messages(self, query: str, limit: int = 50) -> List[dict]:
        """Message-level search — returns individual messages with UUIDs for deep-linking.

        Searches the archive_messages table (which has per-message content) and returns
        results with session_id, message_uuid, snippet, and context.
        """
        # First preprocess the query for better matching
        processed = self._preprocess_nl_query(query)

        conn = self._connect()
        try:
            # Search the sessions FTS for matching sessions, then find specific messages
            try:
                session_rows = conn.execute(
                    """
                    SELECT session_id, title, all_content
                    FROM sessions_fts
                    JOIN sessions s ON s.rowid = sessions_fts.rowid
                    WHERE sessions_fts MATCH ?
                    ORDER BY bm25(sessions_fts, 0, 5.0, 3.0, 1.0, 2.0)
                    LIMIT 30
                    """,
                    (processed,),
                ).fetchall()
            except Exception:
                escaped = '"' + query.replace('"', '""') + '"'
                session_rows = conn.execute(
                    """
                    SELECT session_id, title, all_content
                    FROM sessions_fts
                    JOIN sessions s ON s.rowid = sessions_fts.rowid
                    WHERE sessions_fts MATCH ?
                    ORDER BY bm25(sessions_fts, 0, 5.0, 3.0, 1.0, 2.0)
                    LIMIT 30
                    """,
                    (escaped,),
                ).fetchall()

            results = []
            # For each matching session, scan messages to find the specific ones
            query_words = set(re.findall(r"[a-zA-Z]{3,}", query.lower()))

            for row in session_rows:
                session_id = row["session_id"]
                title = row["title"]
                all_content = row["all_content"] or ""

                # Find the messages from this session that contain the query terms
                # Parse the all_content back into message chunks
                # Since all_content is newline-separated, do a simple scan
                try:
                    messages = list(self.parser._stream_messages(
                        self._get_session_file(session_id)
                    ))
                except Exception:
                    continue

                for msg in messages:
                    if not msg.content or msg.type not in ("user", "assistant"):
                        continue

                    content_lower = msg.content.lower()
                    match_count = sum(1 for w in query_words if w in content_lower)

                    if match_count >= max(1, len(query_words) // 2):
                        # Build snippet around first match
                        snippet = self._build_snippet(msg.content, query_words, context_chars=120)
                        results.append({
                            "session_id": session_id,
                            "session_title": title,
                            "message_uuid": msg.uuid,
                            "message_type": msg.type,
                            "timestamp": msg.timestamp.isoformat(),
                            "snippet": snippet,
                            "match_score": match_count / len(query_words) if query_words else 0,
                        })

            results.sort(key=lambda r: -r["match_score"])
            return results[:limit]
        finally:
            conn.close()

    def _get_session_file(self, session_id: str) -> Optional[Path]:
        """Find the JSONL file for a session."""
        for f in self._get_session_files():
            if f.stem == session_id:
                return f
        return None

    def _build_snippet(self, content: str, query_words: set, context_chars: int = 120) -> str:
        """Build a snippet around the first match of query words in content."""
        content_lower = content.lower()
        best_pos = len(content)
        for word in query_words:
            pos = content_lower.find(word)
            if pos != -1 and pos < best_pos:
                best_pos = pos

        if best_pos == len(content):
            best_pos = 0

        start = max(0, best_pos - context_chars // 2)
        end = min(len(content), best_pos + context_chars)

        snippet = content[start:end].strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(content):
            snippet = snippet + "..."

        # Highlight matches with >>> <<< markers
        for word in query_words:
            pattern = re.compile(re.escape(word), re.IGNORECASE)
            snippet = pattern.sub(lambda m: f">>>{m.group(0)}<<<", snippet)

        return snippet

    def _preprocess_nl_query(self, query: str) -> str:
        """Preprocess a natural language query into FTS5-compatible syntax.

        Handles:
        - Questions like "when did I discuss X" → X
        - Quoted phrases preserved
        - OR/AND operators
        - Synonym expansion for common terms
        """
        original = query.strip()

        # Preserve quoted phrases
        quoted = re.findall(r'"[^"]*"', original)
        remaining = re.sub(r'"[^"]*"', '', original).strip()

        # Strip natural language preamble
        nl_prefixes = [
            r"^(?:when|where|how|what|which|who)\s+(?:did|do|does|was|were|is|are)\s+(?:i|we|you)\s+",
            r"^(?:find|search|show|look\s+for|get)\s+(?:me\s+)?(?:all\s+)?(?:the\s+)?",
            r"^(?:conversations?|sessions?|discussions?|chats?)\s+(?:about|regarding|on|where|with)\s+",
            r"^(?:anything|everything|stuff)\s+(?:about|regarding|on|related\s+to)\s+",
            r"^(?:i\s+)?(?:want|need)\s+(?:to\s+)?(?:find|see|know|search)\s+",
        ]
        for pattern in nl_prefixes:
            remaining = re.sub(pattern, "", remaining, flags=re.IGNORECASE).strip()

        # Strip trailing question marks and filler words
        remaining = re.sub(r"\?+$", "", remaining).strip()
        remaining = re.sub(r"\b(the|a|an|about|regarding|that|which|those|some|any|please)\b", "", remaining, flags=re.IGNORECASE).strip()
        remaining = re.sub(r"\s+", " ", remaining).strip()

        # If we stripped everything, fall back to original
        if not remaining and not quoted:
            remaining = original

        # Combine quoted phrases and remaining terms
        parts = quoted + [remaining] if remaining else quoted
        result = " ".join(parts)

        # If the query has OR, convert to FTS5 OR syntax
        result = re.sub(r"\bor\b", "OR", result, flags=re.IGNORECASE)

        return result if result.strip() else original

    def is_stale(self) -> bool:
        """Check if any JSONL files are newer than the last index run."""
        if not self.db_path.exists():
            return True

        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'last_index_time'"
            ).fetchone()
            if not row:
                return True

            last_index_time = float(row["value"])

            for file_path in self._get_session_files():
                if file_path.stat().st_mtime > last_index_time:
                    return True

            return False
        except Exception:
            return True
        finally:
            conn.close()
