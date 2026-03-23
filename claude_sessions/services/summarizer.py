"""Generate conversation summaries from session messages.

Extracts a structured summary without needing an LLM — uses heuristics
on the user messages to identify topics, flow, and outcomes.
"""

import re
from typing import List, Optional
from ..data.session_parser import SessionParser


def _clean_text(text: str) -> str:
    """Remove XML tags, system content, and excessive whitespace."""
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_user_intent(msg: str, max_len: int = 120) -> Optional[str]:
    """Extract the core intent from a user message."""
    cleaned = _clean_text(msg)
    if len(cleaned) < 5:
        return None

    # Take first sentence or line
    for sep in [". ", "? ", "! ", "\n"]:
        idx = cleaned.find(sep)
        if 0 < idx < max_len:
            return cleaned[: idx + 1].strip()

    if len(cleaned) > max_len:
        return cleaned[:max_len].rstrip() + "..."
    return cleaned


def generate_summary(session_id: str, parser: Optional[SessionParser] = None) -> dict:
    """Generate a structured summary for a session.

    Returns dict with:
        - opening: What the conversation started with
        - topics: List of key topics/questions discussed
        - tools_used: Summary of tools invoked
        - outcome: How the conversation ended
        - topic_count: Number of distinct topics
    """
    if parser is None:
        parser = SessionParser()

    messages = parser.get_session_messages(session_id, limit=2000)
    if not messages:
        return {"opening": None, "topics": [], "tools_used": [], "outcome": None, "topic_count": 0}

    user_msgs = [m for m in messages if m.type == "user" and _clean_text(m.content)]
    asst_msgs = [m for m in messages if m.type == "assistant" and m.content.strip()]

    # Opening: first user message intent
    opening = _extract_user_intent(user_msgs[0].content) if user_msgs else None

    # Topics: extract distinct intents from user messages
    # Skip very similar consecutive messages and system noise
    topics = []
    seen_intents = set()
    for msg in user_msgs:
        intent = _extract_user_intent(msg.content)
        if not intent:
            continue
        # Deduplicate by checking first 40 chars
        key = intent[:40].lower()
        if key in seen_intents:
            continue
        seen_intents.add(key)
        topics.append(intent)

    # Tools used: aggregate tool names from assistant messages
    tool_counts = {}
    for msg in messages:
        if msg.type == "assistant":
            for td in msg.tool_details:
                name = td.name
                # Simplify MCP tool names
                if "__" in name:
                    parts = name.split("__")
                    name = parts[-1] if len(parts) > 1 else name
                tool_counts[name] = tool_counts.get(name, 0) + 1

    # Top tools by frequency
    top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:8]
    tools_used = [f"{name} ({count}x)" for name, count in top_tools]

    # Outcome: last meaningful assistant message (first 200 chars)
    outcome = None
    for msg in reversed(asst_msgs):
        cleaned = _clean_text(msg.content)
        if len(cleaned) > 20:
            outcome = cleaned[:200]
            if len(cleaned) > 200:
                outcome += "..."
            break

    return {
        "opening": opening,
        "topics": topics[:10],  # Cap at 10 topics
        "tools_used": tools_used,
        "outcome": outcome,
        "topic_count": len(topics),
    }
