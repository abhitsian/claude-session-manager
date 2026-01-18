from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import router as api_router
from .data import SessionParser, ActiveSessionDetector
from .config import settings

app = FastAPI(
    title="Claude Session Manager",
    description="View and manage Claude Code sessions",
    version="0.1.0",
)

# Mount static files
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=templates_dir)

# Include API routes
app.include_router(api_router)

# Initialize services
parser = SessionParser()
detector = ActiveSessionDetector()


def format_duration(minutes: int) -> str:
    """Format duration in human-readable form."""
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    if mins == 0:
        return f"{hours}h"
    return f"{hours}h {mins}m"


def format_timestamp(dt) -> str:
    """Format timestamp for display."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
    diff = now - dt

    if diff.days == 0:
        hours = diff.seconds // 3600
        if hours == 0:
            minutes = diff.seconds // 60
            return f"{minutes}m ago"
        return f"{hours}h ago"
    elif diff.days == 1:
        return "Yesterday"
    elif diff.days < 7:
        return f"{diff.days}d ago"
    else:
        return dt.strftime("%b %d")


# Add template filters
templates.env.filters["format_duration"] = format_duration
templates.env.filters["format_timestamp"] = format_timestamp


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    sessions = parser.get_all_sessions()[:20]  # Recent sessions
    active_ids = set(detector.get_active_sessions())
    latest_id = detector.get_latest_session_id()

    # Mark active sessions
    for session in sessions:
        session.is_active = session.session_id in active_ids

    active_sessions = [s for s in sessions if s.is_active]
    recent_sessions = [s for s in sessions if not s.is_active][:10]

    stats = parser.get_stats()
    stats.active_sessions = len(active_ids)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_sessions": active_sessions,
            "recent_sessions": recent_sessions,
            "stats": stats,
            "latest_id": latest_id,
        },
    )


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request, page: int = 1, q: str = ""):
    """Session list page."""
    page_size = 20
    offset = (page - 1) * page_size

    if q:
        sessions = parser.search_sessions(q, search_content=True)
    else:
        sessions = parser.get_all_sessions()

    active_ids = set(detector.get_active_sessions())
    for session in sessions:
        session.is_active = session.session_id in active_ids

    total = len(sessions)
    sessions = sessions[offset : offset + page_size]
    total_pages = (total + page_size - 1) // page_size

    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request,
            "sessions": sessions,
            "page": page,
            "total_pages": total_pages,
            "total": total,
            "query": q,
        },
    )


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str):
    """Session detail page."""
    session = parser.get_session(session_id)
    if not session:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Session not found"},
            status_code=404,
        )

    session.is_active = detector.is_session_active(session_id)
    messages = parser.get_session_messages(session_id, limit=100)
    todos = parser.get_session_todos(session_id)

    return templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "messages": messages,
            "todos": todos,
        },
    )


@app.get("/sessions/{session_id}/context", response_class=HTMLResponse)
async def session_context(request: Request, session_id: str):
    """Context export page."""
    from .services.context_generator import ContextGenerator

    session = parser.get_session(session_id)
    if not session:
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "message": "Session not found"},
            status_code=404,
        )

    context_gen = ContextGenerator(parser, detector)
    context = context_gen.generate_context(session_id)

    return templates.TemplateResponse(
        "context.html",
        {
            "request": request,
            "session": session,
            "context": context,
        },
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "claude_sessions.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
