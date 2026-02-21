"""Main LangGraph workflow for action item extraction."""
import logging
import time
from typing import Literal
from langgraph.graph import StateGraph, END

from .langgraph_state import GraphState
from .langgraph_nodes import (
    segmenter_node,
    relevance_gate_node,
    local_extractor_node,
    evidence_normalizer_node,
    context_resolver_node,
    global_deduplicator_node,
    action_finalizer_node,
    should_continue,
    increment_chunk,
)

logger = logging.getLogger(__name__)


def _timed_node(fn, name: str):
    """Wrap a node so that its execution time is logged."""
    def wrapped(state):
        start = time.perf_counter()
        logger.info("[TIMING] Node %s: started", name)
        try:
            result = fn(state)
            elapsed = time.perf_counter() - start
            logger.info("\033[31m[TIMING] Node %s: completed in %.2fs\033[0m", name, elapsed)
            return result
        except Exception:
            elapsed = time.perf_counter() - start
            logger.exception("\033[31m[TIMING] Node %s: failed after %.2fs\033[0m", name, elapsed)
            raise
    return wrapped


def create_action_extraction_graph():
    """
    Create the LangGraph workflow for action item extraction.
    
    Flow:
    Segmenter → loop:
        RelevanceGate
            if YES:
                LocalExtractor
                EvidenceNormalizer
                ContextResolver
    → GlobalDeduplicator
    → ActionFinalizer
    """
    
    # Create the graph
    workflow = StateGraph(GraphState)
    
    # Add nodes (wrapped with timing)
    workflow.add_node("segmenter", _timed_node(segmenter_node, "segmenter"))
    workflow.add_node("relevance_gate", _timed_node(relevance_gate_node, "relevance_gate"))
    workflow.add_node("local_extractor", _timed_node(local_extractor_node, "local_extractor"))
    workflow.add_node("evidence_normalizer", _timed_node(evidence_normalizer_node, "evidence_normalizer"))
    workflow.add_node("context_resolver", _timed_node(context_resolver_node, "context_resolver"))
    workflow.add_node("global_deduplicator", _timed_node(global_deduplicator_node, "global_deduplicator"))
    workflow.add_node("action_finalizer", _timed_node(action_finalizer_node, "action_finalizer"))
    workflow.add_node("increment_chunk", _timed_node(increment_chunk, "increment_chunk"))
    
    # Set entry point
    workflow.set_entry_point("segmenter")
    
    # Add edges
    workflow.add_edge("segmenter", "relevance_gate")
    
    # Conditional edge from relevance gate
    workflow.add_conditional_edges(
        "relevance_gate",
        should_continue,
        {
            "extract": "local_extractor",
            "next_chunk": "increment_chunk",
            "end": "global_deduplicator",
        }
    )
    
    # Chain: extract → normalize → resolve → increment
    workflow.add_edge("local_extractor", "evidence_normalizer")
    workflow.add_edge("evidence_normalizer", "context_resolver")
    workflow.add_edge("context_resolver", "increment_chunk")
    
    # Loop back to relevance gate
    workflow.add_edge("increment_chunk", "relevance_gate")
    
    # Final processing
    workflow.add_edge("global_deduplicator", "action_finalizer")
    workflow.add_edge("action_finalizer", END)
    
    # Compile the graph
    app = workflow.compile()
    
    logger.info("LangGraph workflow created successfully")
    return app


def extract_actions(transcript_raw: str) -> list:
    """
    Extract action items from a transcript using LangGraph.
    
    Args:
        transcript_raw: Raw transcript text
        
    Returns:
        List of Action objects (as dicts)
    """
    logger.info("Starting action extraction workflow...")
    
    # Create graph
    app = create_action_extraction_graph()
    
    # Initial state
    initial_state: GraphState = {
        "transcript_raw": transcript_raw,
        "chunks": [],
        "chunk_index": 0,
        "candidate_segments": [],
        "unresolved_references": [],
        "active_topics": {},
        "merged_actions": [],
        "emitted_text_spans": set(),
    }
    
    # Run the graph
    logger.info("Executing graph workflow...")
    final_state = app.invoke(initial_state)
    
    # Extract final actions
    actions = final_state.get("merged_actions", [])
    logger.info(f"Extraction complete: {len(actions)} actions extracted")
    
    # Convert to dicts for JSON serialization
    return [action.model_dump() if hasattr(action, 'model_dump') else action for action in actions]
