"""Main LangGraph workflow for action item extraction."""
import logging
import time
from typing import Any, Callable, Optional
from langgraph.graph import StateGraph, END

from .state import GraphState, progress_callback_var
from .nodes import (
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


def create_action_extraction_graph(checkpointer=None):
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

    Parameters
    ----------
    checkpointer:
        Optional LangGraph checkpointer (e.g. PostgresSaver). When provided the
        graph is compiled with persistence enabled so a crashed run can resume
        from the last completed node.
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

    app = workflow.compile(checkpointer=checkpointer) if checkpointer else workflow.compile()
    logger.info("LangGraph workflow created successfully (checkpointer=%s)", type(checkpointer).__name__ if checkpointer else "none")
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


# Node order for streaming progress (must match graph edges)
_EXTRACTOR_NODE_ORDER = (
    "segmenter",
    "parallel_extractor",
    "evidence_normalizer",
    "cross_chunk_resolver",
    "global_deduplicator",
    "action_finalizer",
)


def extract_actions_with_progress(
    transcript_raw: str,
    progress_callback: callable,
) -> list:
    """
    Extract action items from a transcript using LangGraph, streaming progress
    events (step_done per node, progress with current/total for parallel_extractor).

    Args:
        transcript_raw: Raw transcript text.
        progress_callback: Callable(event_type: str, data: dict). Called with
            "progress" and "step_done" events for API SSE.

    Returns:
        List of Action objects serialized as dicts.
    """
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

    # Inject the callback via ContextVar so nodes can reach it without it
    # being stored in (and serialised with) the graph state.
    _token = progress_callback_var.set(progress_callback)
    stream_mode = "values"
    try:
        stream = app.stream(initial_state, stream_mode=stream_mode)
    except TypeError:
        stream = app.stream(initial_state)

    final_state = None
    node_index = 0
    try:
        for state in stream:
            if not isinstance(state, dict):
                continue
            final_state = state
            # LangGraph may yield initial state first (before any node runs). Skip it so we don't
            # emit step_done(segmenter) and step_done(parallel_extractor) too early.
            if node_index == 0 and not state.get("chunks"):
                continue
            if node_index < len(_EXTRACTOR_NODE_ORDER):
                node_name = _EXTRACTOR_NODE_ORDER[node_index]
                progress_callback("step_done", {"agent": "extractor", "step": node_name})
                # Emit "running" for the next step immediately so the frontend doesn't show a gap
                next_index = node_index + 1
                if next_index < len(_EXTRACTOR_NODE_ORDER):
                    next_node = _EXTRACTOR_NODE_ORDER[next_index]
                    progress_callback("progress", {
                        "agent": "extractor",
                        "step": next_node,
                        "status": "running",
                    })
                node_index += 1
    finally:
        progress_callback_var.reset(_token)

    if not final_state:
        return []

    actions = final_state.get("merged_actions", [])
    return [action.model_dump() if hasattr(action, "model_dump") else action for action in actions]


def extract_actions_with_progress_checkpointed(
    transcript_raw: str,
    progress_callback: Callable[[str, dict], Any],
    *,
    checkpointer: Optional[Any] = None,
    thread_id: Optional[str] = None,
    callbacks: Optional[list] = None,
) -> list:
    """Checkpointer-aware variant of :func:`extract_actions_with_progress`.

    When *checkpointer* and *thread_id* are provided the graph is compiled with
    persistence enabled and the run config carries the thread ID so LangGraph
    can resume from the last completed node on retry.

    Parameters
    ----------
    transcript_raw:
        Raw transcript text.
    progress_callback:
        ``Callable(event_type, data)`` — same contract as the non-checkpointed variant.
    checkpointer:
        Optional LangGraph checkpointer (e.g. ``PostgresSaver``).
    thread_id:
        Stable string identifying this run + agent step.  Must be provided when
        *checkpointer* is set.
    callbacks:
        Optional list of LangChain callback handlers (e.g. TokenTrackingCallback).
    """
    app = create_action_extraction_graph(checkpointer=checkpointer)

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

    run_config: dict[str, Any] = {}
    if checkpointer and thread_id:
        run_config["configurable"] = {"thread_id": thread_id}
    if callbacks:
        run_config["callbacks"] = callbacks

    # Set the ContextVar so nodes can emit per-chunk progress without it
    # being stored in (and serialised with) the checkpoint state.
    _token = progress_callback_var.set(progress_callback)
    stream_mode = "values"
    try:
        stream = app.stream(initial_state, config=run_config or None, stream_mode=stream_mode)
    except TypeError:
        stream = app.stream(initial_state, config=run_config or None)

    final_state = None
    node_index = 0
    try:
        for state in stream:
            if not isinstance(state, dict):
                continue
            final_state = state
            if node_index == 0 and not state.get("chunks"):
                continue
            if node_index < len(_EXTRACTOR_NODE_ORDER):
                node_name = _EXTRACTOR_NODE_ORDER[node_index]
                progress_callback("step_done", {"agent": "extractor", "step": node_name})
                next_index = node_index + 1
                if next_index < len(_EXTRACTOR_NODE_ORDER):
                    next_node = _EXTRACTOR_NODE_ORDER[next_index]
                    progress_callback("progress", {
                        "agent": "extractor",
                        "step": next_node,
                        "status": "running",
                    })
                node_index += 1
    finally:
        progress_callback_var.reset(_token)

    if not final_state:
        return []

    actions = final_state.get("merged_actions", [])
    return [action.model_dump() if hasattr(action, "model_dump") else action for action in actions]
