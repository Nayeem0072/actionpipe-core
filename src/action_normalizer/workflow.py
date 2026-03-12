"""LangGraph workflow for the Action Normalizer pipeline."""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Optional

from langgraph.graph import END, StateGraph

from .models import NormalizedAction
from .nodes import (
    action_splitter_node,
    deadline_normalizer_node,
    deduplicator_node,
    tool_classifier_node,
    verb_enricher_node,
)
from .state import NormalizerState

logger = logging.getLogger(__name__)


def _timed_node(fn, name: str):
    """Wrap a node so execution time is logged (mirrors the extractor workflow)."""

    def wrapped(state):
        start = time.perf_counter()
        logger.info("[TIMING] NormalizerNode %s: started", name)
        try:
            result = fn(state)
            elapsed = time.perf_counter() - start
            logger.info(
                "\033[34m[TIMING] NormalizerNode %s: completed in %.2fs\033[0m",
                name,
                elapsed,
            )
            return result
        except Exception:
            elapsed = time.perf_counter() - start
            logger.exception(
                "\033[34m[TIMING] NormalizerNode %s: failed after %.2fs\033[0m",
                name,
                elapsed,
            )
            raise

    return wrapped


def create_normalizer_graph(checkpointer=None):
    """
    Build the Action Normalizer LangGraph workflow.

    Flow (linear, no loops):
      deadline_normalizer
        → verb_enricher       (rule-based + optional LLM for unknown verbs)
        → action_splitter     (rule-based detection + LLM for compound splits)
        → deduplicator        (rule-based similarity)
        → tool_classifier     (rule-based + optional LLM for ambiguous types)
        → END

    Parameters
    ----------
    checkpointer:
        Optional LangGraph checkpointer for fault-tolerant resumption.
    """
    workflow = StateGraph(NormalizerState)

    workflow.add_node("deadline_normalizer", _timed_node(deadline_normalizer_node, "deadline_normalizer"))
    workflow.add_node("verb_enricher", _timed_node(verb_enricher_node, "verb_enricher"))
    workflow.add_node("action_splitter", _timed_node(action_splitter_node, "action_splitter"))
    workflow.add_node("deduplicator", _timed_node(deduplicator_node, "deduplicator"))
    workflow.add_node("tool_classifier", _timed_node(tool_classifier_node, "tool_classifier"))

    workflow.set_entry_point("deadline_normalizer")
    workflow.add_edge("deadline_normalizer", "verb_enricher")
    workflow.add_edge("verb_enricher", "action_splitter")
    workflow.add_edge("action_splitter", "deduplicator")
    workflow.add_edge("deduplicator", "tool_classifier")
    workflow.add_edge("tool_classifier", END)

    app = workflow.compile(checkpointer=checkpointer) if checkpointer else workflow.compile()
    logger.info("Action Normalizer workflow created (checkpointer=%s)", type(checkpointer).__name__ if checkpointer else "none")
    return app


def normalize_actions(
    raw_actions: List[dict],
    meeting_date: Optional[str] = None,
) -> List[dict]:
    """
    Normalize a list of raw action dicts (from the extractor) into tool-ready actions.

    Args:
        raw_actions:   List of Action model dicts (output of the extractor pipeline).
        meeting_date:  ISO 8601 date string used as the reference point for relative
                       deadline resolution (e.g. "2026-03-05").  Defaults to today.

    Returns:
        List of NormalizedAction instances serialised as dicts.
    """
    if not raw_actions:
        logger.info("NormalizerWorkflow: No actions to normalise")
        return []

    logger.info("NormalizerWorkflow: Normalising %d action(s)...", len(raw_actions))

    app = create_normalizer_graph()

    initial_state: NormalizerState = {
        "raw_actions": raw_actions,
        "working_actions": [],
        "meeting_date": meeting_date,
    }

    final_state = app.invoke(initial_state)
    normalized = final_state.get("working_actions", [])
    logger.info("NormalizerWorkflow: Done — %d normalised action(s)", len(normalized))

    return [
        a.model_dump(mode="json") if hasattr(a, "model_dump") else a
        for a in normalized
    ]


# Node order for streaming progress (must match graph edges)
_NORMALIZER_NODE_ORDER = (
    "deadline_normalizer",
    "verb_enricher",
    "action_splitter",
    "deduplicator",
    "tool_classifier",
)


def normalize_actions_with_progress(
    raw_actions: List[dict],
    progress_callback: callable,
    meeting_date: Optional[str] = None,
) -> List[dict]:
    """
    Normalize raw actions with progress events (step_done per node) for SSE.

    Args:
        raw_actions: List of action dicts from the extractor.
        progress_callback: Callable(event_type: str, data: dict). Called with
            "progress" and "step_done" events for API SSE.
        meeting_date: ISO 8601 date for deadline resolution.

    Returns:
        List of normalized action dicts.
    """
    if not raw_actions:
        logger.info("NormalizerWorkflow: No actions to normalise")
        return []

    app = create_normalizer_graph()
    initial_state: NormalizerState = {
        "raw_actions": raw_actions,
        "working_actions": [],
        "meeting_date": meeting_date,
    }

    stream_mode = "values"
    try:
        stream = app.stream(initial_state, stream_mode=stream_mode)
    except TypeError:
        stream = app.stream(initial_state)

    final_state = None
    node_index = 0
    for state in stream:
        if not isinstance(state, dict):
            continue
        final_state = state
        # Skip initial state (no node has run yet: working_actions still empty)
        if node_index == 0 and not state.get("working_actions"):
            continue
        if node_index < len(_NORMALIZER_NODE_ORDER):
            node_name = _NORMALIZER_NODE_ORDER[node_index]
            progress_callback("step_done", {"agent": "normalizer", "step": node_name})
            next_index = node_index + 1
            if next_index < len(_NORMALIZER_NODE_ORDER):
                next_node = _NORMALIZER_NODE_ORDER[next_index]
                progress_callback("progress", {
                    "agent": "normalizer",
                    "step": next_node,
                    "status": "running",
                })
            node_index += 1

    if not final_state:
        return []

    normalized = final_state.get("working_actions", [])
    return [
        a.model_dump(mode="json") if hasattr(a, "model_dump") else a
        for a in normalized
    ]


def normalize_actions_with_progress_checkpointed(
    raw_actions: List[dict],
    progress_callback: Callable[[str, dict], Any],
    *,
    meeting_date: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    thread_id: Optional[str] = None,
    callbacks: Optional[list] = None,
) -> List[dict]:
    """Checkpointer-aware variant of :func:`normalize_actions_with_progress`.

    When *checkpointer* and *thread_id* are provided the graph persists state
    between nodes so a crashed worker can resume from the last completed node.

    Parameters
    ----------
    raw_actions:
        List of action dicts from the extractor.
    progress_callback:
        ``Callable(event_type, data)`` for SSE progress events.
    meeting_date:
        ISO 8601 date string for deadline resolution.
    checkpointer:
        Optional LangGraph checkpointer.
    thread_id:
        Stable thread identifier for this run + agent step.
    callbacks:
        Optional list of LangChain callback handlers.
    """
    if not raw_actions:
        logger.info("NormalizerWorkflow: No actions to normalise")
        return []

    app = create_normalizer_graph(checkpointer=checkpointer)
    initial_state: NormalizerState = {
        "raw_actions": raw_actions,
        "working_actions": [],
        "meeting_date": meeting_date,
    }

    run_config: dict[str, Any] = {}
    if checkpointer and thread_id:
        run_config["configurable"] = {"thread_id": thread_id}
    if callbacks:
        run_config["callbacks"] = callbacks

    stream_mode = "values"
    try:
        stream = app.stream(initial_state, config=run_config or None, stream_mode=stream_mode)
    except TypeError:
        stream = app.stream(initial_state, config=run_config or None)

    final_state = None
    node_index = 0
    for state in stream:
        if not isinstance(state, dict):
            continue
        final_state = state
        if node_index == 0 and not state.get("working_actions"):
            continue
        if node_index < len(_NORMALIZER_NODE_ORDER):
            node_name = _NORMALIZER_NODE_ORDER[node_index]
            progress_callback("step_done", {"agent": "normalizer", "step": node_name})
            next_index = node_index + 1
            if next_index < len(_NORMALIZER_NODE_ORDER):
                next_node = _NORMALIZER_NODE_ORDER[next_index]
                progress_callback("progress", {
                    "agent": "normalizer",
                    "step": next_node,
                    "status": "running",
                })
            node_index += 1

    if not final_state:
        return []

    normalized = final_state.get("working_actions", [])
    return [
        a.model_dump(mode="json") if hasattr(a, "model_dump") else a
        for a in normalized
    ]
