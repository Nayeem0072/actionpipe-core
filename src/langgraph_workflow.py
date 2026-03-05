"""Main LangGraph workflow for action item extraction."""
import logging
import time
from langgraph.graph import StateGraph, END

from .langgraph_state import GraphState
from .langgraph_nodes import (
    segmenter_node,
    parallel_extractor_node,
    evidence_normalizer_node,
    cross_chunk_resolver_node,
    global_deduplicator_node,
    action_finalizer_node,
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

    Flow (linear — no loop):
      segmenter
        → parallel_extractor    (rule-based filter + concurrent LLM extraction)
        → evidence_normalizer   (ASR cleanup, dedup, action object creation)
        → cross_chunk_resolver  (semantic merge of cross-chunk duplicates + pronoun resolution)
        → global_deduplicator   (text-similarity duplicate removal)
        → action_finalizer      (schema enforcement, sort)
        → END
    """
    workflow = StateGraph(GraphState)

    workflow.add_node("segmenter", _timed_node(segmenter_node, "segmenter"))
    workflow.add_node("parallel_extractor", _timed_node(parallel_extractor_node, "parallel_extractor"))
    workflow.add_node("evidence_normalizer", _timed_node(evidence_normalizer_node, "evidence_normalizer"))
    workflow.add_node("cross_chunk_resolver", _timed_node(cross_chunk_resolver_node, "cross_chunk_resolver"))
    workflow.add_node("global_deduplicator", _timed_node(global_deduplicator_node, "global_deduplicator"))
    workflow.add_node("action_finalizer", _timed_node(action_finalizer_node, "action_finalizer"))

    workflow.set_entry_point("segmenter")
    workflow.add_edge("segmenter", "parallel_extractor")
    workflow.add_edge("parallel_extractor", "evidence_normalizer")
    workflow.add_edge("evidence_normalizer", "cross_chunk_resolver")
    workflow.add_edge("cross_chunk_resolver", "global_deduplicator")
    workflow.add_edge("global_deduplicator", "action_finalizer")
    workflow.add_edge("action_finalizer", END)

    app = workflow.compile()
    logger.info("LangGraph workflow created successfully")
    return app


def extract_actions(transcript_raw: str) -> list:
    """
    Extract action items from a transcript using LangGraph.

    Args:
        transcript_raw: Raw transcript text

    Returns:
        List of Action objects serialized as dicts.
    """
    logger.info("Starting action extraction workflow...")

    app = create_action_extraction_graph()

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

    logger.info("Executing graph workflow...")
    final_state = app.invoke(initial_state)

    actions = final_state.get("merged_actions", [])
    logger.info("Extraction complete: %d actions extracted", len(actions))

    return [action.model_dump() if hasattr(action, "model_dump") else action for action in actions]
