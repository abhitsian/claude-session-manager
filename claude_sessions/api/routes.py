from fastapi import APIRouter, Query, HTTPException
from typing import Optional, List

from ..data import SessionParser, ActiveSessionDetector, SessionMetadata
from ..services.context_generator import ContextGenerator

router = APIRouter(prefix="/api")

# Initialize services
parser = SessionParser()
detector = ActiveSessionDetector()
context_gen = ContextGenerator(parser, detector)


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, le=200),
    offset: int = 0,
    active_only: bool = False,
) -> dict:
    """List all sessions with pagination."""
    sessions = parser.get_all_sessions()
    active_ids = set(detector.get_active_sessions())

    # Mark active sessions
    for session in sessions:
        session.is_active = session.session_id in active_ids

    # Filter if needed
    if active_only:
        sessions = [s for s in sessions if s.is_active]

    total = len(sessions)
    sessions = sessions[offset : offset + limit]

    return {
        "sessions": [s.model_dump() for s in sessions],
        "total": total,
        "active_count": len(active_ids),
    }


@router.get("/sessions/active")
async def list_active_sessions() -> dict:
    """List currently active sessions."""
    active_ids = detector.get_active_sessions()
    latest_id = detector.get_latest_session_id()

    sessions = []
    for session_id in active_ids:
        session = parser.get_session(session_id)
        if session:
            session.is_active = True
            sessions.append(session)

    # Sort by last activity
    sessions.sort(key=lambda s: s.last_activity, reverse=True)

    return {
        "sessions": [s.model_dump() for s in sessions],
        "latest_session_id": latest_id,
        "count": len(sessions),
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> dict:
    """Get detailed session metadata."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    session.is_active = detector.is_session_active(session_id)
    todos = parser.get_session_todos(session_id)

    return {
        "session": session.model_dump(),
        "todos": [t.model_dump() for t in todos],
    }


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: str,
    limit: int = Query(100, le=500),
    offset: int = 0,
) -> dict:
    """Get conversation messages for a session."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = parser.get_session_messages(session_id, limit=limit, offset=offset)
    total = session.user_message_count + session.assistant_message_count

    return {
        "messages": [m.model_dump() for m in messages],
        "total": total,
        "has_more": offset + len(messages) < total,
    }


@router.post("/sessions/{session_id}/context")
async def generate_context(
    session_id: str,
    include_files: bool = True,
    include_todos: bool = True,
    max_messages: int = 10,
) -> dict:
    """Generate a context prompt for continuing a session."""
    session = parser.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    context = context_gen.generate_context(
        session_id,
        include_files=include_files,
        include_todos=include_todos,
        max_recent_messages=max_messages,
    )

    return context.model_dump()


@router.get("/stats")
async def get_stats() -> dict:
    """Get aggregated usage statistics."""
    stats = parser.get_stats()
    active_count = len(detector.get_active_sessions())
    stats.active_sessions = active_count
    return stats.model_dump()


@router.get("/search")
async def search_sessions(
    q: str = Query(..., min_length=2),
    search_content: bool = False,
) -> dict:
    """Search sessions by content or metadata."""
    results = parser.search_sessions(q, search_content=search_content)
    active_ids = set(detector.get_active_sessions())

    for session in results:
        session.is_active = session.session_id in active_ids

    return {
        "results": [s.model_dump() for s in results],
        "total": len(results),
        "query": q,
    }
