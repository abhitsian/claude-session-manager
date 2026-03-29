"""Semantic search engine using fastembed + numpy.

Embeds conversation messages into vectors and supports cosine similarity
search. At <10K vectors, brute-force cosine is sub-millisecond — no ANN
index needed.

Storage: numpy .npy files + a JSON metadata sidecar.
"""

import json
import time
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import numpy as np

from ..config import settings
from .session_parser import SessionParser

# Directory for embedding artifacts
EMBED_DIR = Path.home() / ".claude" / "semantic-index"

# Chunk size for embedding — we split long messages into chunks of this size
# to keep embeddings focused and searchable
MAX_CHUNK_CHARS = 800
OVERLAP_CHARS = 100


class SemanticIndex:
    """Semantic search index for Claude Code conversations.

    Stores per-chunk embeddings with metadata for deep-linking back to
    the original message in a session.
    """

    def __init__(self):
        self.embed_dir = EMBED_DIR
        self.embed_dir.mkdir(parents=True, exist_ok=True)
        self.embeddings_path = self.embed_dir / "embeddings.npy"
        self.metadata_path = self.embed_dir / "metadata.json"
        self.state_path = self.embed_dir / "index_state.json"
        self.parser = SessionParser()

        self._model = None
        self._embeddings: Optional[np.ndarray] = None
        self._metadata: Optional[List[dict]] = None

    def _get_model(self):
        """Lazy-load the embedding model."""
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding("BAAI/bge-small-en-v1.5")
        return self._model

    def _load_index(self):
        """Load embeddings and metadata from disk."""
        if self._embeddings is not None:
            return

        if self.embeddings_path.exists() and self.metadata_path.exists():
            self._embeddings = np.load(str(self.embeddings_path))
            with open(self.metadata_path) as f:
                self._metadata = json.load(f)
        else:
            self._embeddings = np.zeros((0, 384), dtype=np.float32)
            self._metadata = []

    def _save_index(self):
        """Persist embeddings and metadata to disk."""
        if self._embeddings is not None:
            np.save(str(self.embeddings_path), self._embeddings)
        if self._metadata is not None:
            with open(self.metadata_path, "w") as f:
                json.dump(self._metadata, f)

    def _save_state(self, state: dict):
        with open(self.state_path, "w") as f:
            json.dump(state, f)

    def _load_state(self) -> dict:
        if self.state_path.exists():
            with open(self.state_path) as f:
                return json.load(f)
        return {}

    def _chunk_message(self, content: str) -> List[str]:
        """Split a message into overlapping chunks for embedding.

        Short messages (<MAX_CHUNK_CHARS) are kept whole.
        Longer messages are split at sentence boundaries with overlap.
        """
        content = content.strip()
        if not content:
            return []

        if len(content) <= MAX_CHUNK_CHARS:
            return [content]

        chunks = []
        pos = 0
        while pos < len(content):
            end = pos + MAX_CHUNK_CHARS

            # Try to break at a sentence boundary
            if end < len(content):
                # Look for sentence end near the chunk boundary
                for sep in [". ", ".\n", "\n\n", "\n", ". ", "? ", "! "]:
                    break_at = content.rfind(sep, pos + MAX_CHUNK_CHARS // 2, end + 50)
                    if break_at != -1:
                        end = break_at + len(sep)
                        break

            chunk = content[pos:end].strip()
            if chunk:
                chunks.append(chunk)

            pos = end - OVERLAP_CHARS
            if pos <= chunks[-1] if not chunks else 0:
                pos = end  # avoid infinite loop

        return chunks

    def _get_session_files(self) -> List[Path]:
        """Get all JSONL session files."""
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

    def build_index(self, force: bool = False) -> dict:
        """Build or rebuild the semantic index.

        Scans all session JSONL files, chunks messages, embeds them,
        and stores the result.

        Returns stats: {chunks_indexed, sessions_indexed, duration_seconds}
        """
        model = self._get_model()
        state = self._load_state()
        last_index_time = state.get("last_index_time", 0)

        session_files = self._get_session_files()

        # Determine which sessions need (re-)indexing
        if force:
            files_to_index = session_files
        else:
            files_to_index = [
                f for f in session_files
                if f.stat().st_mtime > last_index_time
            ]

        if not files_to_index and not force:
            self._load_index()
            return {"chunks_indexed": 0, "sessions_indexed": 0, "duration_seconds": 0, "skipped": True}

        start = time.time()

        # If incremental, load existing index first
        if not force:
            self._load_index()
            # Remove chunks from sessions we're re-indexing
            reindex_session_ids = {f.stem for f in files_to_index}
            if self._metadata:
                keep_mask = [
                    m["session_id"] not in reindex_session_ids
                    for m in self._metadata
                ]
                if any(not k for k in keep_mask):
                    keep_indices = [i for i, k in enumerate(keep_mask) if k]
                    if keep_indices:
                        self._embeddings = self._embeddings[keep_indices]
                        self._metadata = [self._metadata[i] for i in keep_indices]
                    else:
                        self._embeddings = np.zeros((0, 384), dtype=np.float32)
                        self._metadata = []
        else:
            self._embeddings = np.zeros((0, 384), dtype=np.float32)
            self._metadata = []

        # Extract chunks from new/modified sessions
        new_chunks = []
        new_meta = []
        sessions_indexed = 0

        for file_path in files_to_index:
            session_id = file_path.stem
            try:
                messages = list(self.parser._stream_messages(file_path))
            except Exception:
                continue

            session_title = None
            for msg in messages:
                if msg.type == "user" and msg.content and not session_title:
                    # Generate title from first user message
                    from .session_parser import _generate_title
                    session_title = _generate_title(msg.content)

            for msg in messages:
                if msg.type not in ("user", "assistant") or not msg.content:
                    continue

                # Skip very short messages (clarifications like "yes", "no")
                if len(msg.content.strip()) < 20:
                    continue

                chunks = self._chunk_message(msg.content)
                for chunk_idx, chunk in enumerate(chunks):
                    new_chunks.append(chunk)
                    new_meta.append({
                        "session_id": session_id,
                        "session_title": session_title or session_id[:8],
                        "message_uuid": msg.uuid,
                        "message_type": msg.type,
                        "timestamp": msg.timestamp.isoformat(),
                        "chunk_idx": chunk_idx,
                        "chunk_preview": chunk[:120],
                    })

            sessions_indexed += 1

        # Embed all new chunks in batch
        if new_chunks:
            new_embeddings = np.array(list(model.embed(new_chunks)), dtype=np.float32)

            # Append to existing
            if self._embeddings.shape[0] > 0:
                self._embeddings = np.vstack([self._embeddings, new_embeddings])
            else:
                self._embeddings = new_embeddings

            self._metadata.extend(new_meta)

        # Save
        self._save_index()
        self._save_state({
            "last_index_time": time.time(),
            "total_chunks": len(self._metadata),
            "total_sessions": len(set(m["session_id"] for m in self._metadata)),
            "model": "BAAI/bge-small-en-v1.5",
            "dimensions": 384,
        })

        duration = time.time() - start
        return {
            "chunks_indexed": len(new_chunks),
            "sessions_indexed": sessions_indexed,
            "total_chunks": len(self._metadata),
            "duration_seconds": round(duration, 1),
            "skipped": False,
        }

    def search(self, query: str, top_k: int = 20, min_score: float = 0.3) -> List[dict]:
        """Semantic search — find messages most similar to the query.

        Returns list of results with: session_id, session_title, message_uuid,
        message_type, timestamp, chunk_preview, score.
        """
        self._load_index()

        if self._embeddings is None or self._embeddings.shape[0] == 0:
            return []

        model = self._get_model()

        # Embed the query
        query_vec = np.array(list(model.embed([query])), dtype=np.float32)[0]

        # Normalize for cosine similarity
        norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        normalized = self._embeddings / norms

        query_norm = np.linalg.norm(query_vec)
        if query_norm > 0:
            query_vec = query_vec / query_norm

        # Cosine similarity via dot product (both are normalized)
        scores = normalized @ query_vec

        # Get top-k
        top_indices = np.argsort(scores)[::-1][:top_k * 2]  # Get extra for dedup

        results = []
        seen_messages = set()

        for idx in top_indices:
            score = float(scores[idx])
            if score < min_score:
                break

            meta = self._metadata[idx]
            msg_key = (meta["session_id"], meta["message_uuid"])

            # Deduplicate: only show the best chunk per message
            if msg_key in seen_messages:
                continue
            seen_messages.add(msg_key)

            results.append({
                "session_id": meta["session_id"],
                "session_title": meta["session_title"],
                "message_uuid": meta["message_uuid"],
                "message_type": meta["message_type"],
                "timestamp": meta["timestamp"],
                "snippet": meta["chunk_preview"],
                "score": round(score, 4),
                "match_score": score,  # For compatibility with FTS results
            })

            if len(results) >= top_k:
                break

        return results

    def hybrid_search(
        self,
        query: str,
        fts_results: List[dict],
        top_k: int = 20,
        semantic_weight: float = 0.6,
        fts_weight: float = 0.4,
    ) -> List[dict]:
        """Hybrid search combining semantic similarity with FTS5 keyword results.

        Takes FTS results (from SearchIndex.search_messages) and semantic results,
        merges them with weighted scoring, and returns a unified ranked list.
        """
        semantic_results = self.search(query, top_k=top_k * 2)

        # Build a combined score map keyed by (session_id, message_uuid)
        scored = {}

        # Add semantic results
        for r in semantic_results:
            key = (r["session_id"], r["message_uuid"])
            scored[key] = {
                **r,
                "semantic_score": r["score"],
                "fts_score": 0.0,
            }

        # Add/merge FTS results
        if fts_results:
            # Normalize FTS scores to 0-1 range
            max_fts = max(r.get("match_score", 0) for r in fts_results) or 1.0
            for r in fts_results:
                key = (r["session_id"], r["message_uuid"])
                fts_normalized = r.get("match_score", 0) / max_fts
                if key in scored:
                    scored[key]["fts_score"] = fts_normalized
                    # Keep the FTS snippet if it has highlight markers
                    if ">>>" in (r.get("snippet") or ""):
                        scored[key]["snippet"] = r["snippet"]
                else:
                    scored[key] = {
                        **r,
                        "semantic_score": 0.0,
                        "fts_score": fts_normalized,
                    }

        # Compute combined score
        for key, entry in scored.items():
            entry["combined_score"] = (
                semantic_weight * entry.get("semantic_score", 0) +
                fts_weight * entry.get("fts_score", 0)
            )

        # Sort by combined score and return top-k
        ranked = sorted(scored.values(), key=lambda x: -x["combined_score"])
        return ranked[:top_k]

    def get_stats(self) -> dict:
        """Return index statistics."""
        state = self._load_state()
        return {
            "total_chunks": state.get("total_chunks", 0),
            "total_sessions": state.get("total_sessions", 0),
            "model": state.get("model", "not indexed"),
            "dimensions": state.get("dimensions", 0),
            "last_index_time": state.get("last_index_time"),
            "index_exists": self.embeddings_path.exists(),
        }

    def is_stale(self) -> bool:
        """Check if any JSONL files are newer than the last index run."""
        state = self._load_state()
        last_index_time = state.get("last_index_time", 0)
        if last_index_time == 0:
            return True

        for f in self._get_session_files():
            if f.stat().st_mtime > last_index_time:
                return True
        return False
