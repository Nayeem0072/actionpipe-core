"""
Node implementations for the Action Executor pipeline.

  contact_resolver_node  →  mcp_dispatcher_node
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from .mcp_clients import MCPDispatcher
from .state import ExecutorState

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Node 1 — Contact Resolver
# ---------------------------------------------------------------------------

def contact_resolver_node(state: ExecutorState) -> dict[str, Any]:
    """
    Enrich every normalized action's tool_params with real contact details
    pulled from the relation graph (contacts.json).

    Input  state keys: normalized_actions, contacts_path (optional)
    Output state keys: enriched_actions
    """
    start = time.perf_counter()
    from src.relation_graph.resolver import ContactResolver

    contacts_path = state.get("contacts_path")
    resolver = ContactResolver(
        contacts_path=Path(contacts_path) if contacts_path else None
    )

    normalized = state.get("normalized_actions", [])
    enriched: list[dict[str, Any]] = []

    for action in normalized:
        try:
            resolved = resolver.enrich_tool_params(action)
            enriched.append(resolved)
            _log_enrichment(action, resolved)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Contact resolution failed for action %s: %s",
                action.get("id", "?"),
                exc,
            )
            enriched.append(action)

    elapsed = time.perf_counter() - start
    logger.info(
        "contact_resolver_node: enriched %d actions in %.2fs",
        len(enriched),
        elapsed,
    )
    return {"enriched_actions": enriched}


def _log_enrichment(original: dict, resolved: dict) -> None:
    """Log a concise diff of what changed in tool_params."""
    orig_params = original.get("tool_params", {})
    new_params = resolved.get("tool_params", {})
    changes = {
        k: {"before": orig_params.get(k), "after": new_params.get(k)}
        for k in set(list(orig_params.keys()) + list(new_params.keys()))
        if orig_params.get(k) != new_params.get(k)
    }
    if changes:
        logger.debug(
            "Action %s (%s) params enriched: %s",
            resolved.get("id"),
            resolved.get("tool_type"),
            json.dumps(changes, default=str),
        )


# ---------------------------------------------------------------------------
# Node 2 — MCP Dispatcher
# ---------------------------------------------------------------------------

def mcp_dispatcher_node(state: ExecutorState) -> dict[str, Any]:
    """
    Dispatch each enriched action to its MCP server tool.

    Input  state keys: enriched_actions, dry_run
    Output state keys: results
    """
    start = time.perf_counter()
    enriched = state.get("enriched_actions", [])
    dry_run: bool = state.get("dry_run", True)

    dispatcher = MCPDispatcher(dry_run=dry_run)

    if dry_run:
        # Sync path: no asyncio, no process spawn — fast
        results = dispatcher.dispatch_all_sync(enriched)
    else:
        results = asyncio.run(dispatcher.dispatch_all(enriched))

    elapsed = time.perf_counter() - start
    _log_results(results)
    logger.info("mcp_dispatcher_node: completed %d dispatch(es) in %.2fs", len(results), elapsed)
    return {"results": results}


def _log_results(results: list[dict[str, Any]]) -> None:
    success = sum(1 for r in results if r["status"] == "success")
    dry = sum(1 for r in results if r["status"] == "dry_run")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    errors = sum(1 for r in results if r["status"] == "error")

    logger.info(
        "mcp_dispatcher_node: %d success | %d dry_run | %d skipped | %d error",
        success, dry, skipped, errors,
    )
    for r in results:
        if r["status"] == "error":
            logger.error(
                "Action %s failed: %s", r.get("id"), r.get("error")
            )
