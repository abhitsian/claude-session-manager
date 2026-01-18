from datetime import datetime
from typing import Optional, List, Any
from pydantic import BaseModel, Field


class ConversationMessage(BaseModel):
    """A single message in a conversation."""

    uuid: str
    parent_uuid: Optional[str] = None
    type: str  # "user" | "assistant" | "summary"
    timestamp: datetime
    content: str  # Extracted text content
    tool_calls: List[dict] = Field(default_factory=list)
    thinking: Optional[str] = None
    model: Optional[str] = None
    token_usage: Optional[dict] = None


class SessionMetadata(BaseModel):
    """Metadata for a Claude Code session."""

    model_config = {"protected_namespaces": ()}

    session_id: str
    project_path: str
    start_time: datetime
    last_activity: datetime
    message_count: int = 0
    user_message_count: int = 0
    assistant_message_count: int = 0
    model_used: Optional[str] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    summaries: List[str] = Field(default_factory=list)
    is_active: bool = False
    file_path: Optional[str] = None


class SessionStats(BaseModel):
    """Aggregated statistics across all sessions."""

    model_config = {"protected_namespaces": ()}

    total_sessions: int = 0
    total_messages: int = 0
    active_sessions: int = 0
    daily_activity: List[dict] = Field(default_factory=list)
    model_usage: dict = Field(default_factory=dict)
    longest_session: Optional[dict] = None
    first_session_date: Optional[str] = None


class TodoItem(BaseModel):
    """A todo item from a session."""

    content: str
    status: str  # "pending" | "in_progress" | "completed"
    active_form: Optional[str] = None


class Artifact(BaseModel):
    """A file artifact created or modified by Claude."""

    file_path: str
    file_name: str
    file_type: str  # code, document, config, image, etc.
    operation: str  # create, edit
    session_id: str
    timestamp: datetime
    size_bytes: int = 0
    mime_type: str = "application/octet-stream"
    exists: bool = True  # Whether file still exists on disk


class SessionContext(BaseModel):
    """Context export for session continuation."""

    session_id: str
    project_path: str
    start_time: datetime
    last_activity: datetime
    duration_minutes: int
    summary: str
    key_files: List[str] = Field(default_factory=list)
    pending_todos: List[TodoItem] = Field(default_factory=list)
    recent_messages: List[ConversationMessage] = Field(default_factory=list)
    continuation_prompt: str
    resume_command: Optional[str] = None
