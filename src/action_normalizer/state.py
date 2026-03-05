"""State definition for the Action Normalizer LangGraph pipeline."""
from typing import TypedDict, List, Optional

from .models import NormalizedAction


class NormalizerState(TypedDict, total=False):
    """Mutable state passed between normalizer nodes."""

    # Input: raw action dicts or Action model instances from the extractor
    raw_actions: List[dict]

    # In-progress normalized actions — built by deadline_normalizer, then
    # progressively enriched by each subsequent node.
    working_actions: List[NormalizedAction]

    # ISO 8601 date of the meeting (e.g. "2026-03-05").
    # Used as the reference point for relative deadline resolution.
    # Defaults to today when not supplied.
    meeting_date: Optional[str]
