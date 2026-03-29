"""Topic extraction and segmentation engine.

Segments conversations into topic blocks — contiguous stretches of messages
about the same subject. Uses TF-IDF-lite heuristics (no LLM calls) to detect
topic shifts within a single session and cluster topics across sessions.
"""

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import List, Dict, Optional, Tuple

from ..data.session_parser import SessionParser
from ..data.models import SessionMetadata, ConversationMessage


STOP_WORDS = set(
    "the a an is are was were be been being have has had do does did will would "
    "shall should may might can could i you he she it we they me him her us them "
    "my your his its our their this that these those am in on at to for of with "
    "by from as into through during before after above below between about also "
    "just like not but and or so if then than too very much more most other some "
    "any all each every both few many no when where how what which who whom why "
    "here there now then still already yet again once only first last next new "
    "old good great little right big high different small large long just even "
    "let make know think take come see want look give use find tell ask work "
    "seem feel try leave call need become keep start show hear play run move "
    "live believe hold bring happen write provide sit stand lose pay meet "
    "include continue set learn change lead understand watch follow stop create "
    "speak read allow add spend grow open walk win offer remember love consider "
    "appear buy wait serve die send expect build stay fall cut reach kill remain "
    "suggest raise pass sell require report decide pull".split()
)

# Domain-aware topic markers — words that signal specific discussion domains
TOPIC_MARKERS = {
    "career": {"job", "role", "interview", "salary", "compensation", "offer", "position", "hiring", "resume", "recruiter"},
    "code": {"function", "class", "module", "import", "error", "bug", "test", "deploy", "api", "endpoint", "database"},
    "product": {"feature", "user", "design", "spec", "roadmap", "launch", "metric", "kpi", "sprint", "backlog"},
    "writing": {"draft", "article", "email", "document", "memo", "presentation", "slides", "deck", "review"},
    "analysis": {"compare", "evaluate", "analyze", "report", "data", "insight", "trend", "benchmark", "assessment"},
    "planning": {"plan", "strategy", "goal", "timeline", "milestone", "priority", "initiative", "objective"},
    "personal": {"habit", "health", "fitness", "reading", "learning", "hobby", "travel", "family"},
}


class TopicBlock:
    """A contiguous block of messages about the same topic within a session."""

    def __init__(
        self,
        topic_label: str,
        keywords: List[str],
        start_idx: int,
        end_idx: int,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        message_count: int = 0,
        first_message_uuid: Optional[str] = None,
        summary_line: str = "",
        domain: Optional[str] = None,
    ):
        self.topic_label = topic_label
        self.keywords = keywords
        self.start_idx = start_idx
        self.end_idx = end_idx
        self.start_time = start_time
        self.end_time = end_time
        self.message_count = message_count
        self.first_message_uuid = first_message_uuid
        self.summary_line = summary_line
        self.domain = domain

    def to_dict(self) -> dict:
        return {
            "topic_label": self.topic_label,
            "keywords": self.keywords,
            "start_idx": self.start_idx,
            "end_idx": self.end_idx,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "message_count": self.message_count,
            "first_message_uuid": self.first_message_uuid,
            "summary_line": self.summary_line,
            "domain": self.domain,
        }


def _tokenize(text: str) -> List[str]:
    """Extract meaningful tokens from text."""
    tokens = re.findall(r"[a-zA-Z]{3,}", text.lower())
    return [t for t in tokens if t not in STOP_WORDS and len(t) > 3]


def _detect_domain(tokens: List[str]) -> Optional[str]:
    """Detect which domain a set of tokens belongs to."""
    token_set = set(tokens)
    best_domain = None
    best_overlap = 0
    for domain, markers in TOPIC_MARKERS.items():
        overlap = len(token_set & markers)
        if overlap > best_overlap:
            best_overlap = overlap
            best_domain = domain
    return best_domain if best_overlap >= 2 else None


def _generate_topic_label(keywords: List[str], user_msg: str) -> str:
    """Generate a human-readable topic label from keywords and the first user message."""
    # Use the first ~80 chars of the user message as the label, cleaned up
    cleaned = re.sub(r"<[^>]+>", "", user_msg)  # strip XML tags
    cleaned = re.sub(r"```[\s\S]*?```", "[code]", cleaned)  # collapse code blocks
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # If message starts with a slash command, use that
    if cleaned.startswith("/"):
        cmd = cleaned.split()[0] if cleaned.split() else cleaned
        return cmd[:60]

    # Take first sentence or first 80 chars
    first_sentence = re.split(r"[.!?\n]", cleaned)[0].strip()
    if len(first_sentence) > 80:
        first_sentence = first_sentence[:77] + "..."
    if len(first_sentence) < 10:
        # Too short — use keywords instead
        return ", ".join(keywords[:4])
    return first_sentence


def extract_topic_blocks(
    session_id: str, parser: SessionParser, window_size: int = 5
) -> List[TopicBlock]:
    """Segment a session into topic blocks.

    Algorithm:
    1. Slide a window of `window_size` user messages through the conversation
    2. Extract keywords for each window
    3. Detect topic shifts where keyword overlap between adjacent windows drops below threshold
    4. Group contiguous messages into TopicBlocks
    """
    messages = parser.get_session_messages(session_id, limit=2000)
    if not messages:
        return []

    # Get only user messages for topic boundary detection
    user_msgs = [(i, m) for i, m in enumerate(messages) if m.type == "user"]
    if not user_msgs:
        return []

    # If very few user messages, treat entire session as one topic
    if len(user_msgs) <= 2:
        all_tokens = []
        for _, m in user_msgs:
            all_tokens.extend(_tokenize(m.content))
        keywords = [w for w, _ in Counter(all_tokens).most_common(6)]
        first_user = user_msgs[0][1]
        label = _generate_topic_label(keywords, first_user.content)
        domain = _detect_domain(all_tokens)
        return [TopicBlock(
            topic_label=label,
            keywords=keywords,
            start_idx=0,
            end_idx=len(messages) - 1,
            start_time=messages[0].timestamp,
            end_time=messages[-1].timestamp,
            message_count=len(messages),
            first_message_uuid=first_user.uuid,
            summary_line=label,
            domain=domain,
        )]

    # Build keyword windows for each user message
    windows = []
    for idx, (msg_idx, msg) in enumerate(user_msgs):
        tokens = _tokenize(msg.content)
        # Also include next assistant response tokens for richer context
        asst_tokens = []
        for j in range(msg_idx + 1, min(msg_idx + 4, len(messages))):
            if messages[j].type == "assistant":
                asst_tokens.extend(_tokenize(messages[j].content[:500]))
        combined = tokens + asst_tokens
        keyword_counts = Counter(combined)
        windows.append({
            "user_msg_idx": msg_idx,
            "user_msg": msg,
            "keywords": keyword_counts,
            "top_keywords": [w for w, _ in keyword_counts.most_common(10)],
        })

    # Detect topic boundaries by measuring keyword overlap between adjacent windows
    boundaries = [0]  # First message always starts a topic
    for i in range(1, len(windows)):
        prev_kw = set(windows[i - 1]["top_keywords"][:8])
        curr_kw = set(windows[i]["top_keywords"][:8])
        if not prev_kw or not curr_kw:
            boundaries.append(i)
            continue
        overlap = len(prev_kw & curr_kw)
        max_possible = min(len(prev_kw), len(curr_kw))
        similarity = overlap / max_possible if max_possible > 0 else 0

        # Also check for explicit topic shift signals in user message
        content_lower = windows[i]["user_msg"].content.lower().strip()
        explicit_shift = any(content_lower.startswith(p) for p in [
            "now let", "switch to", "different topic", "moving on",
            "next thing", "another question", "unrelated", "new topic",
            "changing gears", "separate question", "also,", "btw ",
            "by the way", "one more thing",
        ])

        # Time gap > 15 minutes between consecutive user messages suggests topic change
        time_gap = (windows[i]["user_msg"].timestamp - windows[i - 1]["user_msg"].timestamp).total_seconds()
        time_shift = time_gap > 900  # 15 minutes

        if similarity < 0.15 or explicit_shift or (similarity < 0.3 and time_shift):
            boundaries.append(i)

    # Build TopicBlocks from boundaries
    blocks = []
    for b_idx, boundary in enumerate(boundaries):
        start_window_idx = boundary
        end_window_idx = boundaries[b_idx + 1] - 1 if b_idx + 1 < len(boundaries) else len(windows) - 1

        # Collect all keywords in this block
        block_keywords = Counter()
        for w_idx in range(start_window_idx, end_window_idx + 1):
            block_keywords.update(windows[w_idx]["keywords"])

        top_kw = [w for w, _ in block_keywords.most_common(6)]

        # Message range in the full messages list
        first_msg_idx = windows[start_window_idx]["user_msg_idx"]
        if end_window_idx + 1 < len(windows):
            last_msg_idx = windows[end_window_idx + 1]["user_msg_idx"] - 1
        else:
            last_msg_idx = len(messages) - 1

        block_messages = messages[first_msg_idx:last_msg_idx + 1]
        first_user = windows[start_window_idx]["user_msg"]
        label = _generate_topic_label(top_kw, first_user.content)
        all_block_tokens = [t for w in range(start_window_idx, end_window_idx + 1) for t in windows[w]["top_keywords"]]
        domain = _detect_domain(all_block_tokens)

        blocks.append(TopicBlock(
            topic_label=label,
            keywords=top_kw,
            start_idx=first_msg_idx,
            end_idx=last_msg_idx,
            start_time=first_user.timestamp,
            end_time=block_messages[-1].timestamp if block_messages else first_user.timestamp,
            message_count=len(block_messages),
            first_message_uuid=first_user.uuid,
            summary_line=label,
            domain=domain,
        ))

    return blocks


def extract_session_topics_summary(
    session_id: str, parser: SessionParser
) -> Dict:
    """Extract a lightweight topic summary for timeline display.

    Returns dict with:
    - topics: list of {label, keywords, domain, message_uuid, time}
    - primary_domain: most common domain across blocks
    """
    blocks = extract_topic_blocks(session_id, parser)
    topics = []
    domain_counts = Counter()

    for block in blocks:
        topics.append({
            "label": block.topic_label,
            "keywords": block.keywords[:4],
            "domain": block.domain,
            "message_uuid": block.first_message_uuid,
            "time": block.start_time.strftime("%H:%M") if block.start_time else None,
            "message_count": block.message_count,
        })
        if block.domain:
            domain_counts[block.domain] += 1

    primary_domain = domain_counts.most_common(1)[0][0] if domain_counts else None

    return {
        "session_id": session_id,
        "topics": topics,
        "topic_count": len(topics),
        "primary_domain": primary_domain,
    }


def cluster_topics_across_sessions(
    session_topics: List[Dict],
) -> List[Dict]:
    """Cluster topics from multiple sessions by keyword similarity.

    Takes output of extract_session_topics_summary() for multiple sessions.
    Returns clusters: [{label, sessions: [{session_id, topic, message_uuid}], keywords}]
    """
    # Flatten all topics with session references
    all_topics = []
    for st in session_topics:
        for topic in st["topics"]:
            all_topics.append({
                "session_id": st["session_id"],
                "label": topic["label"],
                "keywords": set(topic["keywords"]),
                "domain": topic["domain"],
                "message_uuid": topic["message_uuid"],
                "time": topic["time"],
            })

    if not all_topics:
        return []

    # Simple agglomerative clustering by keyword overlap
    clusters = []
    used = set()

    for i, topic in enumerate(all_topics):
        if i in used:
            continue
        cluster = {
            "label": topic["label"],
            "keywords": list(topic["keywords"]),
            "domain": topic["domain"],
            "entries": [{
                "session_id": topic["session_id"],
                "label": topic["label"],
                "message_uuid": topic["message_uuid"],
                "time": topic["time"],
            }],
        }
        used.add(i)

        # Find other topics with similar keywords
        for j, other in enumerate(all_topics):
            if j in used:
                continue
            overlap = len(topic["keywords"] & other["keywords"])
            if overlap >= 2 or (topic["domain"] and topic["domain"] == other["domain"] and overlap >= 1):
                cluster["entries"].append({
                    "session_id": other["session_id"],
                    "label": other["label"],
                    "message_uuid": other["message_uuid"],
                    "time": other["time"],
                })
                used.add(j)

        clusters.append(cluster)

    # Sort by number of entries (most discussed topics first)
    clusters.sort(key=lambda c: -len(c["entries"]))
    return clusters
