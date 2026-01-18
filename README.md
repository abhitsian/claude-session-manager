# Claude Session Manager

A web dashboard for viewing and managing your Claude Code terminal sessions.

## The Problem

When working with Claude Code across multiple terminal windows, managing context becomes challenging:

- **Lost context**: You start a coding session in one terminal, switch to another for a different task, and forget what you were working on in the first
- **No visibility**: There's no easy way to see all your active sessions at a glance or browse past conversations
- **Context switching pain**: Resuming work on a previous session means trying to remember where you left off, what files were changed, and what tasks were pending
- **Scattered history**: Your Claude Code conversations are stored in JSONL files across `~/.claude/`, but there's no interface to browse them

If you regularly use Claude Code across multiple projects or terminal windows, you've likely experienced the frustration of losing track of your sessions and struggling to navigate back to previous work.

## The Solution

Claude Session Manager provides a web-based dashboard that reads directly from your Claude Code session data (`~/.claude/`) and gives you:

- **Active session tracking**: See which sessions are currently running across all your terminal windows
- **Session history**: Browse all past sessions with metadata (date, message count, model used, token usage)
- **Conversation viewer**: Read through any session's full conversation history
- **Context export**: Generate a markdown summary of any session that you can paste into a new Claude session to continue where you left off
- **Search**: Find sessions by content or project path

## Screenshots

The dashboard shows active sessions with a green indicator and recent sessions below:

```
┌─────────────────────────────────────────────────────────┐
│  ACTIVE NOW (2)                                         │
│  🟢 c28719c6... | /Users/you/project | 2h ago | 248 msgs│
│  🟢 046f05b5... | /Users/you/other   | 5m ago | 136 msgs│
├─────────────────────────────────────────────────────────┤
│  RECENT SESSIONS                                        │
│  ○ e94a724f... | Jan 12 | 241 msgs | claude-opus-4.5   │
│  ○ 9d247872... | Jan 11 | 423 msgs | claude-sonnet-4.5 │
└─────────────────────────────────────────────────────────┘
```

## Installation

```bash
git clone https://github.com/abhitsian/claude-session-manager.git
cd claude-session-manager
pip install -r requirements.txt
```

## Usage

Start the server:

```bash
python -m uvicorn claude_sessions.main:app --reload --port 8080
```

Open http://localhost:8080 in your browser.

## Features

### Dashboard
- View active sessions across all terminals
- Quick stats: total sessions, messages, token usage
- Recent session list with one-click access

### Session Browser
- Paginated list of all sessions
- Search by content or project path
- Filter by active/inactive status

### Conversation Viewer
- Full message history with user/assistant distinction
- Tool call indicators
- Collapsible extended thinking blocks
- Token usage per message

### Context Export
Generate a continuation prompt to resume work in a new session:

```markdown
# Session Context Continuation

## Original Session
- Session ID: c28719c6-9910-495d-a57b-59bac975a319
- Project: /Users/you/project
- Started: 2026-01-18 02:20
- Messages: 248 (81 user, 167 assistant)

## Session Summary
[Summary of work done]

## Pending Tasks
- [ ] Implement feature X
- [ ] Fix bug in Y

## Continue From Here
Please continue working on this session...
```

Copy this to your clipboard and paste into a new Claude session to provide context.

## How It Works

Claude Code stores all session data in `~/.claude/`:

| Location | Content |
|----------|---------|
| `projects/{project}/{sessionId}.jsonl` | Full conversation history |
| `history.jsonl` | Global activity log |
| `debug/latest` | Symlink to active session |
| `stats-cache.json` | Aggregated usage statistics |
| `todos/{sessionId}.json` | Task lists per session |

This app reads these files directly (read-only) and presents them in a web interface. No data is modified or sent anywhere.

## Tech Stack

- **Backend**: Python, FastAPI
- **Frontend**: Jinja2 templates, Tailwind CSS, HTMX
- **Data**: Direct JSONL/JSON parsing (no database)

## License

MIT
