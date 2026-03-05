"""Data models for the Action Normalizer pipeline."""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class ToolType(str, Enum):
    SEND_EMAIL = "send_email"
    CREATE_JIRA_TASK = "create_jira_task"
    SET_CALENDAR = "set_calendar"
    CREATE_NOTION_DOC = "create_notion_doc"
    SEND_NOTIFICATION = "send_notification"
    GENERAL_TASK = "general_task"


class NormalizedAction(BaseModel):
    """A fully normalized, tool-ready action item."""

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4())[:8],
        description="Short unique identifier for this action",
    )
    description: str = Field(description="Clean, atomic description of the action")
    assignee: Optional[str] = Field(default=None, description="Who is responsible")
    raw_deadline: Optional[str] = Field(
        default=None, description="Original deadline string from the extractor"
    )
    normalized_deadline: Optional[str] = Field(
        default=None, description="ISO 8601 date (YYYY-MM-DD) or null"
    )
    speaker: str = Field(description="Who mentioned this action in the meeting")
    verb: str = Field(description="Upgraded, meaningful action verb")
    confidence: float = Field(default=0.5, description="Confidence score 0.0-1.0")
    tool_type: ToolType = Field(
        default=ToolType.GENERAL_TASK,
        description="Which tool should execute this action",
    )
    tool_params: dict = Field(
        default_factory=dict,
        description="Tool-specific parameters extracted from the description",
    )
    source_spans: list[str] = Field(
        default_factory=list,
        description="Span IDs from the original transcript",
    )
    parent_id: Optional[str] = Field(
        default=None,
        description="ID of the compound action this was split from, if applicable",
    )
    meeting_window: Optional[tuple[int, int]] = Field(
        default=None,
        description="(start_chunk, end_chunk) where this action was mentioned",
    )
    action_category: Optional[str] = Field(
        default=None,
        description="Category hint propagated from the extractor",
    )
    topic_tags: list[str] = Field(
        default_factory=list,
        description="Subject keywords for semantic matching",
    )
