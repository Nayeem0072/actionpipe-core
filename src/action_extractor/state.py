"""Graph state definition for LangGraph action item extraction."""
import contextvars
from typing import Callable, Dict, Any, List, Optional, Set, TypedDict

from .models import Segment, Action


# ---------------------------------------------------------------------------
# Progress callback — injected via ContextVar, NOT stored in graph state.
#
# Storing a Callable in GraphState would break PostgresSaver checkpointing
# because functions cannot be serialised by msgpack/pickle/JSON.
# Using a ContextVar keeps the callback accessible inside nodes without it
# ever appearing in the checkpoint payload.  It is also thread-safe: each
# Celery worker thread runs in its own execution context.
# ---------------------------------------------------------------------------
progress_callback_var: contextvars.ContextVar[Optional[Callable[[str, dict], None]]] = \
    contextvars.ContextVar("extractor_progress_callback", default=None)


class GraphState(TypedDict, total=False):
    """Global state for the LangGraph workflow."""
    transcript_raw: str  # Original transcript (optional, only at start)
    chunks: List[str]  # List of transcript chunks
    chunk_index: int  # Current chunk being processed
    relevance_result: str  # Result from relevance gate: "YES", "NO", or "DONE"

    # Raw signals
    candidate_segments: List[Segment]  # Segments extracted from current chunk

    # Memory across chunks
    unresolved_references: List[Segment]  # Segments with unresolved references
    active_topics: Dict[str, Any]  # Topic tracking for context resolution

    # Final structures
    merged_actions: List[Action]  # Accumulated actions
    emitted_text_spans: Set[str]  # Anti-loop memory: track processed spans
