# Claude Desk

The UI for Claude Code.

See all your conversations, track costs, get prompting insights, and never lose a session again.

## The Problem

Claude Code deletes conversation files after ~30 days. You can't search across sessions. You don't know what you're spending. You can't see patterns in how you use Claude. Every conversation is a black box that eventually disappears.

## What This Does

**See everything** — All your Claude conversations in one place. Dashboard, timeline, full-text search across every session you've ever had. Sessions that Claude Code deleted are still here.

**Track costs** — Per-session cost breakdown with daily trends. Know exactly what you're spending and which sessions cost the most.

**Get better at prompting** — Analyzes your actual usage patterns and generates a personalized prompting score with actionable recommendations. Detects when you're pasting too much, writing vague prompts, or going in clarification loops.

**Never lose a session** — A daily cron job archives sessions to SQLite before Claude Code deletes them. Full conversation content, tool calls, everything — permanently stored and searchable.

**Fork conversations** — Branch from any point in a conversation to explore a different direction without polluting the original thread.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/abhitsian/claude-desk/main/get.sh | bash
```

That's it. This:
- Dashboard server at `http://localhost:8080` (auto-starts on login)
- Daily archiver cron (runs at 3 AM, saves expiring sessions)
- Initial archive of all current sessions

## Features

### Dashboard
- Active, starred, and recent sessions
- Auto-generated titles from first message
- Pasted content detection (job postings, comp data, emails)
- Star/favorite sessions for quick access
- Resume any session directly in Terminal

### Timeline
- Sessions grouped by day
- Full history going back to your first Claude Code session

### Conversation Viewer
- Chat-style layout (you on right, Claude on left)
- Collapsible threads — collapse all to see one-line summaries
- Markdown rendering with tables, code blocks, headers
- Tool calls with details (what Claude read, wrote, searched)
- Fork from any message to explore a different direction
- Export/copy as markdown

### Insights
- Total cost with daily bar chart
- Most expensive sessions ranked
- Prompting score (0-100) based on your patterns
- Personalized recommendations with before/after examples
- "Apply to CLAUDE.md" — one click to inject prompting guidelines into every future session
- Usage patterns: peak hours, session length distribution, model usage

### Search
- Full-text search across all sessions (live + archived)
- SQLite FTS5 — instant results across hundreds of sessions

### Artifacts
- Every file Claude created or modified
- Filter by type (code, document, config, web)
- Track which session created each file

## How It Works

```
~/.claude/
├── projects/{project}/{sessionId}.jsonl    ← Live sessions (Claude manages)
├── history.jsonl                           ← Prompt history (all sessions ever)
├── session-archive.db                      ← Permanent archive (we manage)
└── session-search.db                       ← FTS index (we manage)
```

- The dashboard reads JSONL files directly (read-only)
- The daily cron archives sessions older than 25 days to SQLite
- Sessions deleted by Claude Code are served from the archive
- `history.jsonl` fills in metadata for sessions we never archived

### The Self-Learning Loop

The insights engine analyzes your prompting patterns and generates a personalized playbook. Click "Apply to CLAUDE.md" and these rules become part of every future Claude session:

```
You type a prompt
    → Hook checks prompt quality (vague? pasted content? repeated topic?)
    → Shows suggestions before Claude sees it
    → Claude reads your CLAUDE.md rules and adapts
    → Session gets archived + analyzed
    → Playbook updates → CLAUDE.md evolves
```

## Prompt Enhancer Hook

An optional Claude Code hook that analyzes your prompts before they go to Claude:

- Warns when prompts are too vague
- Detects large pasted content without instructions
- Finds related prior sessions and suggests `/recall`

Installed automatically by `install.sh` if you want it (edit `~/.claude/settings.json` to enable/disable).

## Screenshots

### Dashboard
![Dashboard](screenshots/dashboard.png)

### Dashboard (Light Mode)
![Dashboard Light](screenshots/dashboard-light.png)

### Timeline
![Timeline](screenshots/timeline.png)

### Session Detail
![Session Detail](screenshots/session-detail.png)

### Chat-Style Conversation Viewer
![Conversation](screenshots/conversation.png)

### Collapsible Threads
![Collapsed](screenshots/collapsed.png)

### Insights & Cost Tracking
![Insights](screenshots/insights.png)

### Prompting Score & Recommendations
![Prompting Score](screenshots/prompting-score.png)

### Artifacts Browser
![Artifacts](screenshots/artifacts.png)

## Tech Stack

- **Backend**: Python, FastAPI, SQLite (FTS5)
- **Frontend**: Jinja2, Tailwind CSS, HTMX, marked.js
- **Design**: Editorial aesthetic with Playfair Display + DM Sans
- **No external services**: Everything runs locally, no data leaves your machine

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.claude.desk.plist
launchctl unload ~/Library/LaunchAgents/com.claude.session-archiver.plist
rm ~/Library/LaunchAgents/com.claude.desk.plist
rm ~/Library/LaunchAgents/com.claude.session-archiver.plist
```

Your archive data stays in `~/.claude/session-archive.db` until you delete it.

## License

MIT
