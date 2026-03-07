"""Graph state definition for LangGraph action item extraction."""
from typing import TypedDict, List, Dict, Any, Set, Optional, Callable

from .models import Segment, Action


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

    # Optional progress callback for API: (event_type: str, data: dict) -> None
    progress_callback: Optional[Callable[[str, dict], None]]
