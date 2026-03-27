import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import router as api_router
from .data import SessionParser, ActiveSessionDetector, ArtifactParser, SearchIndex, SessionMetadata
from .data import favorites
from .data.archive import SessionArchive
from .data.history_reader import get_all_session_history
from .config import settings

app = FastAPI(
    title="Claude Desk",
    description="View and manage UI for Claude Code",
    version="0.2.0",
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
artifact_parser = ArtifactParser()
search_index = SearchIndex()
archive = SessionArchive()


@app.on_event("startup")
async def startup_event():
    """Build search index on startup."""
    if search_index.is_stale():
        search_index.build_index()
    # Update which archived sessions are still live
    archive._mark_gone_sessions()


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


def _archived_to_metadata(a: dict) -> SessionMetadata:
    """Convert an archive row dict to SessionMetadata."""
    return SessionMetadata(
        session_id=a["session_id"],
        project_path=a.get("project_path") or "",
        start_time=datetime.fromisoformat(a["start_time"]),
        last_activity=datetime.fromisoformat(a["last_activity"]),
        message_count=a["message_count"],
        user_message_count=a.get("user_message_count", 0),
        assistant_message_count=a.get("assistant_message_count", 0),
        model_used=a.get("model_used"),
        total_input_tokens=a.get("total_input_tokens", 0),
        total_output_tokens=a.get("total_output_tokens", 0),
        title=a.get("title"),
        has_pasted_content=bool(a.get("has_pasted_content", 0)),
        pasted_content_types=json.loads(a.get("pasted_content_types") or "[]"),
    )


def _get_all_sessions_unified(limit: int = 200) -> list:
    """Get ALL sessions ever — live JSONL + archived DB + history.jsonl metadata."""
    # 1. Live sessions (have JSONL files)
    live = parser.get_all_sessions()
    seen_ids = {s.session_id for s in live}

    # 2. Archived sessions (JSONL deleted but we saved content)
    archived = archive.get_archived_only(limit=500)
    for a in archived:
        if a["session_id"] not in seen_ids:
            seen_ids.add(a["session_id"])
            live.append(_archived_to_metadata(a))

    # 3. History-only sessions (JSONL deleted, not archived, but history.jsonl has prompts)
    all_history = get_all_session_history()
    for sid, entry in all_history.items():
        if sid in seen_ids:
            continue
        seen_ids.add(sid)
        if entry["start_time"] and entry["last_activity"]:
            live.append(SessionMetadata(
                session_id=sid,
                project_path=entry.get("project_path") or "",
                start_time=entry["start_time"],
                last_activity=entry["last_activity"],
                message_count=entry.get("message_count", 0),
                title=entry.get("title"),
            ))

    live.sort(key=lambda s: s.last_activity, reverse=True)
    return live[:limit]


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page — shows live + archived sessions unified."""
    sessions = _get_all_sessions_unified(limit=20)
    active_ids = set(detector.get_active_sessions())
    latest_id = detector.get_latest_session_id()

    for session in sessions:
        session.is_active = session.session_id in active_ids

    active_sessions = [s for s in sessions if s.is_active]
    recent_sessions = [s for s in sessions if not s.is_active][:10]

    stats = parser.get_stats()
    stats.active_sessions = len(active_ids)
    # Show total from unified view, not just live JSONL
    all_unified = _get_all_sessions_unified()
    stats.total_sessions = len(all_unified)
    stats.total_messages = sum(s.message_count for s in all_unified)

    # Archive status
    archive_stats = archive.get_stats()
    last_archive_ts = archive.get_last_archive_time()
    if last_archive_ts:
        last_archive_dt = datetime.fromtimestamp(last_archive_ts)
        last_archive_time = format_timestamp(last_archive_dt)
        last_archive_full = last_archive_dt.strftime("%Y-%m-%d %H:%M")
    else:
        last_archive_time = None
        last_archive_full = None

    # Get favorites
    fav_ids = {f["session_id"] for f in favorites.get_favorites()}

    # Get favorited sessions that might not be in recent
    fav_sessions = []
    if fav_ids:
        for fav_id in fav_ids:
            session = parser.get_session(fav_id)
            if session:
                session.is_active = session.session_id in active_ids
                fav_sessions.append(session)
        fav_sessions.sort(key=lambda s: s.last_activity, reverse=True)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_sessions": active_sessions,
            "recent_sessions": recent_sessions,
            "fav_sessions": fav_sessions,
            "fav_ids": fav_ids,
            "stats": stats,
            "latest_id": latest_id,
            "archive_stats": archive_stats,
            "last_archive_time": last_archive_time,
            "last_archive_full": last_archive_full,
        },
    )


@app.get("/timeline", response_class=HTMLResponse)
async def timeline(request: Request):
    """Timeline view — all sessions (live + archived) grouped by day."""
    sessions = _get_all_sessions_unified(limit=200)
    active_ids = set(detector.get_active_sessions())
    fav_ids = {f["session_id"] for f in favorites.get_favorites()}

    for session in sessions:
        session.is_active = session.session_id in active_ids

    days: OrderedDict = OrderedDict()
    for s in sessions:
        day_key = s.start_time.strftime("%Y-%m-%d")
        day_label = s.start_time.strftime("%A, %B %d, %Y")
        if day_key not in days:
            days[day_key] = {"label": day_label, "sessions": [], "date": day_key}
        days[day_key]["sessions"].append(s)

    return templates.TemplateResponse(
        "timeline.html",
        {
            "request": request,
            "days": list(days.values()),
            "fav_ids": fav_ids,
        },
    )


@app.get("/sessions", response_class=HTMLResponse)
async def sessions_list(request: Request, page: int = 1, q: str = "", show: str = ""):
    """Session list page with FTS search. Includes archived sessions."""
    page_size = 20
    offset = (page - 1) * page_size

    if q:
        # Search across both live and archived sessions
        archive_results = archive.search(q, limit=100)
        # Build session objects from archive results
        sessions = []
        seen_ids = set()
        for result in archive_results:
            sid = result["session_id"]
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            # Try live session first
            session = parser.get_session(sid)
            if not session:
                # Build from archive data
                archived = archive.get_archived_session(sid)
                if archived:
                    session = SessionMetadata(
                        session_id=sid,
                        project_path=archived["project_path"] or "",
                        start_time=datetime.fromisoformat(archived["start_time"]),
                        last_activity=datetime.fromisoformat(archived["last_activity"]),
                        message_count=archived["message_count"],
                        user_message_count=archived.get("user_message_count", 0),
                        assistant_message_count=archived.get("assistant_message_count", 0),
                        model_used=archived.get("model_used"),
                        title=archived.get("title"),
                        has_pasted_content=bool(archived.get("has_pasted_content", 0)),
                        pasted_content_types=json.loads(archived.get("pasted_content_types") or "[]"),
                    )
            if session:
                sessions.append(session)
    else:
        # Show everything — live + archived, unified
        sessions = _get_all_sessions_unified(limit=200)

    active_ids = set(detector.get_active_sessions())
    fav_ids = {f["session_id"] for f in favorites.get_favorites()}
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
            "fav_ids": fav_ids,
        },
    )


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
async def session_detail(request: Request, session_id: str):
    """Session detail page. Falls back to archive if JSONL is gone."""
    session = parser.get_session(session_id)
    from_archive = False

    if session:
        session.is_active = detector.is_session_active(session_id)
        conv_tree = parser.get_conversation_tree(session_id)
        todos = parser.get_session_todos(session_id)
    else:
        # Try archive
        archived = archive.get_archived_session(session_id)
        if not archived:
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "message": "Session not found"},
                status_code=404,
            )
        from_archive = True
        # Build a SessionMetadata-like object from archive
        from .data.models import SessionMetadata, ConversationThread, ConversationMessage, ToolCallDetail
        session = SessionMetadata(
            session_id=session_id,
            project_path=archived["project_path"] or "",
            start_time=datetime.fromisoformat(archived["start_time"]),
            last_activity=datetime.fromisoformat(archived["last_activity"]),
            message_count=archived["message_count"],
            user_message_count=archived["user_message_count"],
            assistant_message_count=archived["assistant_message_count"],
            model_used=archived["model_used"],
            total_input_tokens=archived["total_input_tokens"],
            total_output_tokens=archived["total_output_tokens"],
            title=archived["title"],
            has_pasted_content=bool(archived["has_pasted_content"]),
            pasted_content_types=json.loads(archived["pasted_content_types"] or "[]"),
        )

        # Build threads from archived messages
        archived_msgs = archive.get_archived_messages(session_id)
        threads = []
        current_thread = None
        for msg_data in archived_msgs:
            tool_details = []
            try:
                for td in json.loads(msg_data.get("tool_details_json") or "[]"):
                    tool_details.append(ToolCallDetail(
                        name=td.get("name", ""),
                        input_summary=td.get("input_summary"),
                        file_path=td.get("file_path"),
                        command=td.get("command"),
                    ))
            except Exception:
                pass

            msg = ConversationMessage(
                uuid=msg_data["uuid"] or "",
                parent_uuid=msg_data.get("parent_uuid"),
                type=msg_data["type"],
                timestamp=datetime.fromisoformat(msg_data["timestamp"]),
                content=msg_data["content"] or "",
                tool_details=tool_details,
                thinking=msg_data.get("thinking"),
                model=msg_data.get("model"),
                is_sidechain=bool(msg_data.get("is_sidechain", 0)),
            )

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

        from .data.models import ConversationTree
        conv_tree = ConversationTree(threads=threads, total_messages=len(archived_msgs))
        todos = []

    is_fav = favorites.is_favorite(session_id)

    # Get or generate summary
    summary = search_index.get_summary(session_id)
    if not summary:
        from .services.summarizer import generate_summary
        summary = generate_summary(session_id, parser)
        search_index.save_summary(session_id, summary)

    # Cost + efficiency insights for this session
    from .services.insights import calculate_session_cost, analyze_prompt_efficiency, find_related_sessions
    cost = calculate_session_cost(session)
    efficiency = analyze_prompt_efficiency(session_id, parser) if not from_archive else {}

    # Related sessions
    all_sessions = parser.get_all_sessions()
    related = find_related_sessions(session_id, all_sessions, parser, top_n=4) if not from_archive else []

    return templates.TemplateResponse(
        "session_detail.html",
        {
            "request": request,
            "session": session,
            "threads": conv_tree.threads,
            "branch_points": conv_tree.branch_points if hasattr(conv_tree, 'branch_points') else 0,
            "todos": todos,
            "is_favorite": is_fav,
            "summary": summary,
            "from_archive": from_archive,
            "cost": cost,
            "efficiency": efficiency,
            "related": related,
        },
    )


@app.get("/sessions/{session_id}/markdown", response_class=PlainTextResponse)
async def session_markdown(request: Request, session_id: str):
    """Export session as markdown."""
    session = parser.get_session(session_id)
    if not session:
        return PlainTextResponse("Session not found", status_code=404)

    messages = parser.get_session_messages(session_id, limit=2000)
    title = session.title or session_id[:8]

    lines = [
        f"# {title}",
        "",
        f"**Session ID:** `{session_id}`",
        f"**Project:** `{session.project_path}`",
        f"**Started:** {session.start_time.strftime('%Y-%m-%d %H:%M')}",
        f"**Last Activity:** {session.last_activity.strftime('%Y-%m-%d %H:%M')}",
        f"**Model:** {session.model_used or 'Unknown'}",
        f"**Messages:** {session.message_count}",
        "",
        "---",
        "",
    ]

    for msg in messages:
        role = "**You**" if msg.type == "user" else "**Claude**"
        time_str = msg.timestamp.strftime("%H:%M")
        lines.append(f"### {role} ({time_str})")
        lines.append("")
        lines.append(msg.content)
        lines.append("")

        if msg.tool_details:
            for td in msg.tool_details:
                lines.append(f"> Tool: `{td.name}` — {td.input_summary or ''}")
            lines.append("")

        lines.append("---")
        lines.append("")

    md = "\n".join(lines)

    return PlainTextResponse(
        md,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="session-{session_id[:8]}.md"'
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


# ===== Favorites API =====


@app.post("/api/resume/{session_id}")
async def resume_session(session_id: str):
    """Open Terminal.app and run claude --resume for this session."""
    import subprocess
    cmd = f'claude --resume {session_id}'
    apple_script = f'''
    tell application "Terminal"
        activate
        do script "{cmd}"
    end tell
    '''
    try:
        subprocess.Popen(["osascript", "-e", apple_script])
        return JSONResponse({"ok": True, "session_id": session_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/fork")
async def fork_session(request: Request):
    """Fork a conversation — writes context file and opens claude in Terminal."""
    import subprocess
    import tempfile
    body = await request.json()
    prompt = body.get("fork_prompt", "")
    source = body.get("source_session", "")
    context = body.get("context", "")

    # Build the initial prompt for the forked session
    fork_text = f"I'm forking from a previous conversation (session {source[:8]}).\n\n"
    fork_text += "Here's the context from that conversation:\n\n"
    fork_text += context
    if prompt:
        fork_text += f"\n\n---\n\nNow I want to explore a different direction: {prompt}"
    else:
        fork_text += "\n\n---\n\nPlease continue from this context."

    # Write to a temp file
    fork_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="claude-fork-",
        dir="/tmp", delete=False
    )
    fork_file.write(fork_text)
    fork_file.close()

    # Open Terminal and run claude with the fork file piped as first message
    cmd = f'cat {fork_file.name} | claude'
    apple_script = f'''
    tell application "Terminal"
        activate
        do script "{cmd}"
    end tell
    '''
    try:
        subprocess.Popen(["osascript", "-e", apple_script])
        return JSONResponse({"ok": True, "fork_file": fork_file.name})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/apply-playbook")
async def apply_playbook(request: Request):
    """Append prompting playbook to the user's CLAUDE.md."""
    body = await request.json()
    content = body.get("content", "")
    if not content:
        return JSONResponse({"ok": False, "error": "No content"})

    claude_md = Path.home() / ".claude" / "CLAUDE.md"
    marker = "# Prompting Guidelines (auto-generated from usage patterns)"

    try:
        existing = claude_md.read_text() if claude_md.exists() else ""

        # Remove old playbook block if present
        if marker in existing:
            before = existing[:existing.index(marker)].rstrip()
            # Find the end of the block (next # heading or end of file)
            after_start = existing.index(marker) + len(marker)
            remaining = existing[after_start:]
            # Find next top-level heading
            next_heading = remaining.find("\n# ")
            if next_heading >= 0:
                after = remaining[next_heading:]
            else:
                after = ""
            existing = (before + "\n\n" + after).strip()

        # Append new playbook
        new_content = existing + "\n\n" + content + "\n"
        claude_md.write_text(new_content)
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@app.post("/api/favorites/{session_id}/toggle")
async def toggle_favorite(session_id: str):
    """Toggle favorite status for a session."""
    new_state = favorites.toggle_favorite(session_id)
    return JSONResponse({"favorited": new_state, "session_id": session_id})


@app.get("/insights", response_class=HTMLResponse)
async def insights_page(request: Request, days: int = 7):
    """Cross-conversation insights and cost analysis."""
    from .services.insights import (
        calculate_period_costs, analyze_cross_session_patterns,
        calculate_session_cost, generate_prompting_playbook,
    )

    all_sessions = _get_all_sessions_unified(limit=500)

    # Period costs
    costs = calculate_period_costs(all_sessions, days=days)

    # Cross-session patterns
    patterns = analyze_cross_session_patterns(all_sessions, parser)

    # Personalized prompting playbook
    live_sessions = parser.get_all_sessions()
    playbook = generate_prompting_playbook(live_sessions, parser)

    # Per-session costs for the table
    session_costs = []
    for s in all_sessions[:50]:
        c = calculate_session_cost(s)
        c["title"] = s.title
        c["session_id"] = s.session_id
        c["date"] = s.start_time.strftime("%b %d")
        session_costs.append(c)

    return templates.TemplateResponse(
        "insights.html",
        {
            "request": request,
            "costs": costs,
            "patterns": patterns,
            "playbook": playbook,
            "session_costs": session_costs,
            "days": days,
        },
    )


@app.get("/skills", response_class=HTMLResponse)
async def skills_page(request: Request):
    """Skills browser — all commands and skills from user + plugins."""
    from .services.skill_scanner import scan_skills, group_by_source, get_stats

    all_skills = scan_skills()
    groups = group_by_source(all_skills)
    stats = get_stats(all_skills)

    return templates.TemplateResponse(
        "skills.html",
        {
            "request": request,
            "groups": groups,
            "stats": stats,
        },
    )


@app.get("/visualize", response_class=HTMLResponse)
async def visualize_page(request: Request):
    """Rich visualizations — 3D terrain + constellation scatter."""
    return templates.TemplateResponse(
        "visualize.html",
        {"request": request},
    )


@app.get("/artifacts", response_class=HTMLResponse)
async def artifacts_list(request: Request, file_type: str = "", session_id: str = ""):
    """Artifacts listing page."""
    artifacts = artifact_parser.get_all_artifacts(limit=200)

    if file_type:
        artifacts = [a for a in artifacts if a.file_type == file_type]

    if session_id:
        artifacts = [a for a in artifacts if a.session_id == session_id]

    stats = artifact_parser.get_artifact_stats()

    all_artifacts = artifact_parser.get_all_artifacts(limit=500)
    file_types = sorted(set(a.file_type for a in all_artifacts))

    return templates.TemplateResponse(
        "artifacts.html",
        {
            "request": request,
            "artifacts": artifacts,
            "stats": stats,
            "file_types": file_types,
            "current_type": file_type,
            "current_session": session_id,
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
