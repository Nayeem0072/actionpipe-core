"""
MCP dispatcher — connects to MCP servers and calls the appropriate tool
for each enriched NormalizedAction.

Live mode (dry_run=False):
  Uses langchain-mcp-adapters MultiServerMCPClient to launch MCP server
  processes (via stdio) and invoke their tools as LangChain ToolCall objects.

Dry-run mode (dry_run=True, default):
  Skips all process spawning; returns a structured preview of what *would*
  be called so the rest of the pipeline can be tested without credentials.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MCP_CONFIG_PATH = Path(__file__).parent.parent.parent / "mcp_config.json"


def _load_mcp_config(config_path: Optional[Path] = None) -> dict:
    path = config_path or _MCP_CONFIG_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_env_vars(env_dict: dict[str, str]) -> dict[str, str]:
    """Expand ${VAR} placeholders from the process environment."""
    resolved = {}
    for key, value in env_dict.items():
        if value.startswith("${") and value.endswith("}"):
            var_name = value[2:-1]
            resolved[key] = os.environ.get(var_name, "")
        else:
            resolved[key] = value
    return resolved


class MCPDispatcher:
    """
    Routes each enriched action to the correct MCP server tool.

    Parameters
    ----------
    dry_run:
        When True (default), simulate tool calls without launching MCP processes.
    config_path:
        Path to mcp_config.json. Defaults to the project-root file.
    """

    def __init__(
        self,
        dry_run: bool = True,
        config_path: Optional[Path] = None,
    ) -> None:
        self.dry_run = dry_run
        self._config = _load_mcp_config(config_path)
        self._tool_type_map: dict[str, Optional[str]] = self._config.get(
            "toolTypeToServer", {}
        )
        self._servers: dict[str, dict] = self._config.get("mcpServers", {})

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def dispatch(
        self, action: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Execute the MCP tool call for a single enriched action.

        Returns a result dict:
          {id, tool_type, server, mcp_tool, params, status, response, error}
        """
        action_id: str = action.get("id", "unknown")
        tool_type: str = action.get("tool_type", "general_task")
        params: dict = action.get("tool_params", {})

        server_name = self._tool_type_map.get(tool_type)
        if not server_name:
            return self._result(
                action_id, tool_type, None, None, params,
                status="skipped",
                response=None,
                error=f"No MCP server configured for tool_type '{tool_type}'",
            )

        server_cfg = self._servers.get(server_name, {})
        mcp_tool_name: str = server_cfg.get("_mcpTool", tool_type)

        if self.dry_run:
            return self._dry_run_result(
                action_id, tool_type, server_name, mcp_tool_name, params
            )

        return await self._live_dispatch(
            action_id, tool_type, server_name, server_cfg, mcp_tool_name, params
        )

    def _dispatch_one_dry(self, action: dict[str, Any]) -> dict[str, Any]:
        """Synchronous single-action dry-run (no process spawn, no asyncio)."""
        action_id: str = action.get("id", "unknown")
        tool_type: str = action.get("tool_type", "general_task")
        params: dict = action.get("tool_params", {})

        server_name = self._tool_type_map.get(tool_type)
        if not server_name:
            return self._result(
                action_id, tool_type, None, None, params,
                status="skipped",
                response=None,
                error=f"No MCP server configured for tool_type '{tool_type}'",
            )

        server_cfg = self._servers.get(server_name, {})
        mcp_tool_name: str = server_cfg.get("_mcpTool", tool_type)
        return self._dry_run_result(
            action_id, tool_type, server_name, mcp_tool_name, params
        )

    def dispatch_all_sync(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Dispatch all actions synchronously. For dry_run only; no process spawn, no asyncio.
        Use this from the executor node when dry_run=True to avoid event-loop overhead.
        """
        if not self.dry_run:
            raise RuntimeError("dispatch_all_sync is for dry_run only")
        return [self._dispatch_one_dry(a) for a in actions]

    async def dispatch_all(
        self, actions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Dispatch every action and collect results. In live mode, reuses one MCP client for the batch."""
        if self.dry_run:
            return self.dispatch_all_sync(actions)

        # Live mode: one client for all servers, dispatch all actions without respawning
        return await self._dispatch_all_live(actions)

    async def _dispatch_all_live(
        self, actions: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Run all actions in live mode using a single MCP client (one process per server)."""
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore
        except ImportError:
            return [
                self._result(
                    a.get("id", "?"), a.get("tool_type", "general_task"),
                    None, None, a.get("tool_params", {}),
                    status="error", response=None,
                    error="langchain-mcp-adapters is not installed. Run: pip install langchain-mcp-adapters",
                )
                for a in actions
            ]

        server_spec: dict = {}
        for name, cfg in self._servers.items():
            server_spec[name] = {
                "command": cfg.get("command", "npx"),
                "args": cfg.get("args", []),
                "env": _resolve_env_vars(cfg.get("env", {})),
                "transport": "stdio",
            }

        results: list[dict[str, Any]] = []
        try:
            async with MultiServerMCPClient(server_spec) as client:
                tools = client.get_tools()
                tools_by_name = {t.name: t for t in tools}

                for action in actions:
                    action_id = action.get("id", "unknown")
                    tool_type = action.get("tool_type", "general_task")
                    params = action.get("tool_params", {})
                    server_name = self._tool_type_map.get(tool_type)
                    if not server_name:
                        results.append(self._result(
                            action_id, tool_type, None, None, params,
                            status="skipped", response=None,
                            error=f"No MCP server configured for tool_type '{tool_type}'",
                        ))
                        continue

                    server_cfg = self._servers.get(server_name, {})
                    mcp_tool_name = server_cfg.get("_mcpTool", tool_type)
                    tool = tools_by_name.get(mcp_tool_name)
                    if tool is None:
                        available = list(tools_by_name.keys())
                        results.append(self._result(
                            action_id, tool_type, server_name, mcp_tool_name, params,
                            status="error", response=None,
                            error=f"Tool '{mcp_tool_name}' not found. Available: {available}",
                        ))
                        continue

                    try:
                        response = await tool.ainvoke(params)
                        results.append(self._result(
                            action_id, tool_type, server_name, mcp_tool_name, params,
                            status="success", response=response, error=None,
                        ))
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("MCP dispatch failed for action %s", action_id)
                        results.append(self._result(
                            action_id, tool_type, server_name, mcp_tool_name, params,
                            status="error", response=None, error=str(exc),
                        ))

        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP client failed")
            for action in actions:
                if len(results) >= len(actions):
                    break
                results.append(self._result(
                    action.get("id", "?"), action.get("tool_type", "general_task"),
                    None, None, action.get("tool_params", {}),
                    status="error", response=None, error=str(exc),
                ))

        return results

    # ------------------------------------------------------------------
    # Dry-run
    # ------------------------------------------------------------------

    def _dry_run_result(
        self,
        action_id: str,
        tool_type: str,
        server_name: str,
        mcp_tool_name: str,
        params: dict,
    ) -> dict[str, Any]:
        logger.info(
            "[DRY RUN] Would call MCP server=%s tool=%s params=%s",
            server_name,
            mcp_tool_name,
            json.dumps(params, default=str),
        )
        return self._result(
            action_id, tool_type, server_name, mcp_tool_name, params,
            status="dry_run",
            response={"preview": f"Would invoke {server_name}/{mcp_tool_name}"},
            error=None,
        )

    # ------------------------------------------------------------------
    # Live dispatch via langchain-mcp-adapters
    # ------------------------------------------------------------------

    async def _live_dispatch(
        self,
        action_id: str,
        tool_type: str,
        server_name: str,
        server_cfg: dict,
        mcp_tool_name: str,
        params: dict,
    ) -> dict[str, Any]:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore
        except ImportError as exc:
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, params,
                status="error",
                response=None,
                error=(
                    "langchain-mcp-adapters is not installed. "
                    "Run: pip install langchain-mcp-adapters"
                ),
            )

        server_spec = {
            server_name: {
                "command": server_cfg.get("command", "npx"),
                "args": server_cfg.get("args", []),
                "env": _resolve_env_vars(server_cfg.get("env", {})),
                "transport": "stdio",
            }
        }

        try:
            async with MultiServerMCPClient(server_spec) as client:
                tools = client.get_tools()
                tool = next(
                    (t for t in tools if t.name == mcp_tool_name), None
                )
                if tool is None:
                    available = [t.name for t in tools]
                    return self._result(
                        action_id, tool_type, server_name, mcp_tool_name, params,
                        status="error",
                        response=None,
                        error=f"Tool '{mcp_tool_name}' not found. Available: {available}",
                    )

                response = await tool.ainvoke(params)
                return self._result(
                    action_id, tool_type, server_name, mcp_tool_name, params,
                    status="success",
                    response=response,
                    error=None,
                )

        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP dispatch failed for action %s", action_id)
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, params,
                status="error",
                response=None,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Result builder
    # ------------------------------------------------------------------

    @staticmethod
    def _result(
        action_id: str,
        tool_type: str,
        server: Optional[str],
        mcp_tool: Optional[str],
        params: dict,
        *,
        status: str,
        response: Any,
        error: Optional[str],
    ) -> dict[str, Any]:
        return {
            "id": action_id,
            "tool_type": tool_type,
            "server": server,
            "mcp_tool": mcp_tool,
            "params": params,
            "status": status,
            "response": response,
            "error": error,
        }
