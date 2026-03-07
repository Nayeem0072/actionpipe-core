"""
LangGraph workflow for the Action Executor pipeline.

  contact_resolver_node  →  mcp_dispatcher_node
"""
from __future__ import annotations

from typing import Any, Callable

from langgraph.graph import END, StateGraph

from .nodes import contact_resolver_node, mcp_dispatcher_node
from .state import ExecutorState

# Node order for streaming progress (must match graph edges)
_EXECUTOR_NODE_ORDER = ("contact_resolver", "mcp_dispatcher")


def build_executor_graph() -> StateGraph:
    """Construct and compile the two-node executor graph."""
    graph = StateGraph(ExecutorState)

    graph.add_node("contact_resolver", contact_resolver_node)
    graph.add_node("mcp_dispatcher", mcp_dispatcher_node)

    graph.set_entry_point("contact_resolver")
    graph.add_edge("contact_resolver", "mcp_dispatcher")
    graph.add_edge("mcp_dispatcher", END)

    return graph.compile()


def execute_actions(
    normalized_actions: list[dict[str, Any]],
    *,
    dry_run: bool = True,
    contacts_path: str | None = None,
) -> list[dict[str, Any]]:
    """
    Run the full executor pipeline on a list of NormalizedAction dicts.

    Parameters
    ----------
    normalized_actions:
        Output from the normalizer stage (list of dicts matching NormalizedAction schema).
    dry_run:
        When True (default), simulate MCP calls without launching real processes.
    contacts_path:
        Optional path to an alternative contacts.json (useful for testing).

    Returns
    -------
    List of result dicts: {id, tool_type, server, mcp_tool, params, status, response, error}
    """
    graph = build_executor_graph()

    initial_state: ExecutorState = {
        "normalized_actions": normalized_actions,
        "dry_run": dry_run,
    }
    if contacts_path:
        initial_state["contacts_path"] = contacts_path

    final_state = graph.invoke(initial_state)
    return final_state.get("results", [])


def execute_actions_with_progress(
    normalized_actions: list[dict[str, Any]],
    emit_cb: Callable[[str, dict], None],
    *,
    dry_run: bool = True,
    contacts_path: str | None = None,
) -> list[dict[str, Any]]:
    """
    Run the executor pipeline and emit progress events for the Run/SSE API.

    emit_cb(event_type, data) is called with "progress" and "step_done" for
    agent="executor", step="contact_resolver" | "mcp_dispatcher".

    Returns
    -------
    List of result dicts: {id, tool_type, server, mcp_tool, params, status, response, error}
    """
    if not normalized_actions:
        return []

    graph = build_executor_graph()
    initial_state: ExecutorState = {
        "normalized_actions": normalized_actions,
        "dry_run": dry_run,
    }
    if contacts_path:
        initial_state["contacts_path"] = contacts_path

    stream_mode = "values"
    try:
        stream = graph.stream(initial_state, stream_mode=stream_mode)
    except TypeError:
        stream = graph.stream(initial_state)

    final_state = None
    node_index = 0
    for state in stream:
        if not isinstance(state, dict):
            continue
        final_state = state
        # Skip initial state (no node has run yet: enriched_actions not yet set)
        if node_index == 0 and not state.get("enriched_actions"):
            continue
        if node_index < len(_EXECUTOR_NODE_ORDER):
            node_name = _EXECUTOR_NODE_ORDER[node_index]
            emit_cb("step_done", {"agent": "executor", "step": node_name})
            next_index = node_index + 1
            if next_index < len(_EXECUTOR_NODE_ORDER):
                next_node = _EXECUTOR_NODE_ORDER[next_index]
                emit_cb("progress", {
                    "agent": "executor",
                    "step": next_node,
                    "status": "running",
                })
            node_index += 1

    if not final_state:
        return []
    return final_state.get("results", [])
