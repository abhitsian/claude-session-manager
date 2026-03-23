#!/usr/bin/env python3
"""Claude Code Hook: Prompt Enhancer

Runs on every user-prompt-submit. Analyzes the prompt against
learned patterns and provides suggestions or auto-enhancements.

This is the "intelligent middleman" — it reads your prompting
playbook and prior sessions to improve prompts before Claude sees them.

Exit codes:
  0 = allow prompt through (with optional modifications via stdout JSON)
  2 = block prompt and show message to user
"""

import json
import os
import re
import sqlite3
import sys
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
ARCHIVE_DB = CLAUDE_DIR / "session-archive.db"
SEARCH_DB = CLAUDE_DIR / "session-search.db"


def get_input():
    """Read the hook input from stdin."""
    return json.loads(sys.stdin.read())


def search_prior_sessions(query: str, limit: int = 3) -> list:
    """Quick FTS search against the archive."""
    if not ARCHIVE_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(ARCHIVE_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT session_id, title,
                      snippet(archive_fts, 2, '>>>', '<<<', '...', 32) AS snippet
               FROM archive_fts
               JOIN archived_sessions USING (session_id)
               WHERE archive_fts MATCH ?
               ORDER BY rank LIMIT ?""",
            (f'"{query}"', limit),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def analyze_prompt(prompt_text: str) -> dict:
    """Analyze the prompt for common anti-patterns."""
    issues = []
    suggestions = []

    text = prompt_text.strip()
    length = len(text)

    # 1. Too vague / short
    if length < 40 and not any(c in text for c in ["/", "```", "http"]):
        issues.append("vague")
        suggestions.append(
            "Your prompt is very short. Consider adding: "
            "[context about your role/situation] + [specific question] + [desired format]."
        )

    # 2. Large pasted content without instruction
    lines = text.split("\n")
    if length > 2000:
        # Check if the first few lines contain an instruction
        first_200 = text[:200].lower()
        has_instruction = any(
            kw in first_200
            for kw in ["help", "look at", "review", "analyze", "tell me", "evaluate",
                        "compare", "create", "build", "fix", "what", "how", "why",
                        "should", "can you", "please"]
        )
        if not has_instruction:
            issues.append("paste_without_instruction")
            suggestions.append(
                "You're pasting large content without a clear instruction at the top. "
                "Add a 1-line directive before the pasted content so Claude knows what to do with it."
            )

    # 3. Check for prior related sessions
    # Extract key terms for search
    words = re.findall(r"[a-zA-Z]{4,}", text[:500].lower())
    # Use most distinctive words (skip common ones)
    common = {"this", "that", "with", "from", "have", "been", "what", "they", "your",
              "about", "would", "could", "should", "there", "their", "which", "will",
              "some", "when", "make", "like", "just", "over", "also", "into", "more"}
    key_terms = [w for w in words if w not in common][:5]
    query = " ".join(key_terms)

    related = []
    if query and len(key_terms) >= 2:
        related = search_prior_sessions(query, limit=2)

    if related:
        titles = ", ".join(f'"{r["title"][:40]}"' for r in related)
        suggestions.append(
            f"You have prior conversations on this topic: {titles}. "
            f"Consider using /recall to load that context instead of re-explaining."
        )

    return {
        "issues": issues,
        "suggestions": suggestions,
        "related_sessions": related,
        "prompt_length": length,
    }


def main():
    hook_input = get_input()
    session_id = hook_input.get("session_id", "")
    prompt_content = ""

    # Extract text content from the message
    message = hook_input.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        prompt_content = content
    elif isinstance(content, list):
        prompt_content = " ".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )

    if not prompt_content.strip():
        # Empty or non-text message, let it through
        sys.exit(0)

    analysis = analyze_prompt(prompt_content)

    # If there are suggestions, output them as a note (non-blocking)
    if analysis["suggestions"]:
        # Print suggestions to stderr (shown to user as hook output)
        note = " | ".join(analysis["suggestions"])
        print(json.dumps({
            "message": f"💡 {note}"
        }))
        # Exit 0 = let the prompt through (don't block)
        sys.exit(0)

    # No issues, let it through silently
    sys.exit(0)


if __name__ == "__main__":
    main()
