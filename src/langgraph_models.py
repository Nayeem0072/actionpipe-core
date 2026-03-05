"""Data models for LangGraph action item extraction system."""
from typing import Optional, Literal
from pydantic import BaseModel, Field
from datetime import datetime


class ActionDetails(BaseModel):
    """Action details for action items."""
    description: str | None = Field(default=None, description="Description of the action")
    assignee: str | None = Field(default=None, description="Who is assigned to do the action")
    deadline: str | None = Field(default=None, description="Deadline or timeline mentioned")
    confidence: float | None = Field(default=None, description="Confidence score (0.0-1.0)")
    topic_tags: list[str] = Field(
        default_factory=list,
        description="2-4 short subject keywords independent of verb/phrasing, e.g. ['client', 'email', 'scope']",
    )
    unresolved_reference: str | None = Field(
        default=None,
        description="When context_unclear=True, a short phrase describing what is being referenced, e.g. 'the migration task'",
    )


class Segment(BaseModel):
    """A segment extracted from a transcript chunk."""
    speaker: str = Field(description="Speaker name")
    text: str = Field(description="Exact text from transcript")
    intent: Literal[
        "suggestion",
        "information",
        "question",
        "decision",
        "action_item",
        "agreement",
        "clarification",
    ] = Field(description="Conversational intent")
    resolved_context: str = Field(
        default="",
        description="What earlier topic this refers to, if applicable"
    )
    context_unclear: bool = Field(
        default=False,
        description="True if reference cannot be resolved"
    )
    action_details: Optional[ActionDetails] = Field(
        default=None,
        description="Action details (only for action_item intent)"
    )
    span_id: str = Field(
        default="",
        description="Unique identifier for this text span"
    )
    chunk_index: int = Field(
        default=-1,
        description="Which chunk this segment came from"
    )
    raw_verb: str | None = Field(
        default=None,
        description="Raw verb phrase before normalization"
    )


class Action(BaseModel):
    """Final action item extracted from transcript."""
    description: str = Field(description="Clear description of the action")
    assignee: str | None = Field(default=None, description="Who is assigned")
    deadline: str | None = Field(default=None, description="Deadline or timeline")
    speaker: str = Field(description="Who mentioned this action")
    verb: str = Field(description="Normalized verb (e.g., 'fix', 'send', 'review')")
    object_text: str | None = Field(default=None, description="What the action is about")
    confidence: float = Field(default=0.5, description="Confidence score 0.0-1.0")
    source_spans: list[str] = Field(
        default_factory=list,
        description="Span IDs that contributed to this action"
    )
    meeting_window: tuple[int, int] | None = Field(
        default=None,
        description="(start_chunk, end_chunk) where this action was mentioned"
    )
    topic_tags: list[str] = Field(
        default_factory=list,
        description="Subject keywords for cross-chunk semantic matching, e.g. ['client', 'email', 'scope']",
    )
    unresolved_reference: str | None = Field(
        default=None,
        description="Short phrase describing an unresolved cross-chunk reference, e.g. 'the migration task'",
    )
