"""
MCP dispatcher — connects to MCP servers and calls the appropriate tool
for each enriched NormalizedAction.

Live mode (dry_run=False):
  Uses langchain-mcp-adapters MultiServerMCPClient to launch MCP server
  processes (via stdio) and invoke their tools as LangChain ToolCall objects.

Dry-run mode (dry_run=True, default):
  Skips all process spawning; returns a structured preview of what *would*
  be called so the rest of the pipeline can be tested without credentials.

Sandbox: only tools listed in allowedTools (mcp_config.json) are invoked.
Parameter validation and mapping (e.g. Slack message_hint -> text) reduce
prompt injection and ensure required MCP params are present.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_MCP_CONFIG_PATH = Path(__file__).parent.parent.parent / "mcp_config.json"

# region agent log
_DEBUG_LOG_PATH = Path(__file__).parent.parent.parent / ".cursor" / "debug.log"


def _write_debug_log(message: str, data: dict[str, Any], hypothesis_id: str) -> None:
    """Append one NDJSON debug log line to the shared debug file."""
    try:
        ts_ms = int(time.time() * 1000)
        payload = {
            "id": f"log_{ts_ms}",
            "timestamp": ts_ms,
            "location": "src/action_executor/mcp_clients.py",
            "message": message,
            "data": data,
            "runId": data.get("runId") or "unknown",
            "hypothesisId": hypothesis_id,
        }
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        return


# endregion

# Max length for Slack message text (prompt-injection / spam mitigation)
_SLACK_TEXT_MAX_LEN = 4000

# Heuristic: reject leading phrases that look like instruction override
_PROMPT_INJECTION_PATTERN = re.compile(
    r"^\s*(ignore\s+previous\s+instructions|follow\s+these\s+instructions|you\s+are\s+now)",
    re.IGNORECASE,
)


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


def _validate_and_map_slack_params(params: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """
    Map our executor params to Slack MCP slack_post_message schema and validate.

    MCP expects: channel_id (str), text (str).
    We have: recipient, channel, message_hint, recipient_display_name, etc.

    Returns (mapped_params, error_message). If error_message is set, mapped_params
    is still the best-effort dict for logging.
    """
    channel_id = (
        (params.get("recipient") or params.get("channel") or "")
        if isinstance(params.get("recipient"), str)
        else str(params.get("recipient") or params.get("channel") or "")
    )
    text_raw = params.get("message_hint") or params.get("text") or ""
    if not isinstance(text_raw, str):
        text_raw = str(text_raw)
    text = text_raw[: _SLACK_TEXT_MAX_LEN].strip()

    # region agent log
    _write_debug_log(
        "slack_param_mapping",
        {
            "runId": params.get("run_id") or params.get("runId") or "unknown",
            "recipient": params.get("recipient"),
            "channel": params.get("channel"),
            "recipientDisplayName": params.get("recipient_display_name"),
            "channelIdDerived": channel_id,
            "textPreview": text[:120],
        },
        hypothesis_id="H4",
    )
    # endregion
    if _PROMPT_INJECTION_PATTERN.match(text):
        return (
            {"channel_id": channel_id, "text": text},
            "Message content rejected by sandbox (instruction override pattern)",
        )
    if not channel_id or not channel_id.strip():
        return ({"channel_id": channel_id, "text": text}, "Slack channel_id is required")
    if not text:
        return ({"channel_id": channel_id, "text": text}, "Slack text (message) is required")
    return ({"channel_id": channel_id, "text": text}, None)


def _slack_result_params(mapped_params: dict[str, Any], original_params: dict[str, Any]) -> dict[str, Any]:
    """
    Build params for the API response so the frontend gets display fields.
    MCP is only called with channel_id + text; we merge back recipient_display_name,
    recipient, channel, message_hint for display.
    """
    out = dict(mapped_params)
    for key in ("recipient_display_name", "recipient", "channel", "message_hint"):
        if original_params.get(key) is not None:
            out[key] = original_params[key]
    return out


def _is_slack_user_id(channel_id: str) -> bool:
    """Return True if channel_id looks like a Slack user ID (U...) rather than a channel ID (C/D/G...)."""
    if not channel_id or not isinstance(channel_id, str):
        return False
    s = channel_id.strip()
    return len(s) >= 9 and s.startswith("U")


async def _resolve_slack_user_to_dm_channel(token: str, user_id: str) -> Optional[str]:
    """
    Call Slack conversations.open with the user ID to get the DM channel ID.
    Returns the channel id (D...) or None on failure.
    Requires bot scope im:write.
    """
    if not token or not user_id:
        return None
    url = "https://slack.com/api/conversations.open"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"users": user_id.strip()}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, data=payload)
        data = resp.json()
        if data.get("ok") and isinstance(data.get("channel"), dict):
            return data["channel"].get("id")
        logger.warning(
            "Slack conversations.open failed for user %s: %s",
            user_id,
            data.get("error", resp.text),
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Slack conversations.open error for user %s: %s", user_id, exc)
        return None


def _parse_slack_mcp_response(response: Any) -> tuple[bool, Optional[str]]:
    """
    Parse MCP tool response from Slack (list of content items with type/text).
    Returns (ok, error_message). If Slack API returned ok:false, error_message is set.
    """
    if not response or not isinstance(response, list):
        return True, None
    for item in response:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if not text:
                continue
            try:
                data = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and "ok" in data:
                if data.get("ok") is True:
                    return True, None
                return False, data.get("error", "unknown")
    return True, None


class MCPDispatcher:
    """
    Routes each enriched action to the correct MCP server tool.

    Parameters
    ----------
    dry_run:
        When True (default), simulate tool calls without launching MCP processes.
    config_path:
        Path to mcp_config.json. Defaults to the project-root file.
    server_env_overrides:
        Optional dict mapping server name -> env dict to merge over base config
        (e.g. {"slack": {"SLACK_BOT_TOKEN": "xoxb-user-token"}} for per-request token).
    """

    def __init__(
        self,
        dry_run: bool = True,
        config_path: Optional[Path] = None,
        server_env_overrides: Optional[dict[str, dict[str, str]]] = None,
    ) -> None:
        self.dry_run = dry_run
        self._config = _load_mcp_config(config_path)
        self._tool_type_map: dict[str, Optional[str]] = self._config.get(
            "toolTypeToServer", {}
        )
        self._servers: dict[str, dict] = self._config.get("mcpServers", {})
        self._allowed_tools: dict[str, list[str]] = self._config.get(
            "allowedTools", {}
        )
        self._server_env_overrides = server_env_overrides or {}

    def _check_allowlist(self, server_name: str, mcp_tool_name: str) -> Optional[str]:
        """Return None if allowed, else an error message."""
        allowed = self._allowed_tools.get(server_name)
        if allowed is None:
            return "Tool not allowed by sandbox (server not in allowlist)"
        if mcp_tool_name not in allowed:
            return f"Tool '{mcp_tool_name}' not allowed by sandbox. Allowed for {server_name}: {allowed}"
        return None

    def _resolve_server_env(self, server_name: str) -> dict[str, str]:
        """Base env from config, merged with server_env_overrides for this server."""
        base = _resolve_env_vars(
            self._servers.get(server_name, {}).get("env", {})
        )
        overrides = self._server_env_overrides.get(server_name)
        if overrides:
            base = {**base, **overrides}
        return base

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _sandbox_params(
        self,
        server_name: str,
        mcp_tool_name: str,
        params: dict[str, Any],
    ) -> tuple[dict[str, Any], Optional[str]]:
        """
        Apply allowlist and server-specific param validation/mapping.
        Returns (params_to_use, error_message). If error is set, params_to_use
        may still be the mapped params for logging.
        """
        allow_err = self._check_allowlist(server_name, mcp_tool_name)
        if allow_err:
            return (params, allow_err)
        if server_name == "slack" and mcp_tool_name == "slack_post_message":
            mapped, val_err = _validate_and_map_slack_params(params)
            if val_err:
                return (mapped, val_err)
            return (mapped, None)
        return (params, None)

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
        params: dict = action.get("tool_params", action.get("params", {}))

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

        params_to_use, sandbox_err = self._sandbox_params(
            server_name, mcp_tool_name, params
        )
        result_params = (
            _slack_result_params(params_to_use, params)
            if server_name == "slack" and mcp_tool_name == "slack_post_message"
            else params_to_use
        )
        if sandbox_err:
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, result_params,
                status="error", response=None, error=sandbox_err,
            )

        if self.dry_run:
            return self._dry_run_result(
                action_id, tool_type, server_name, mcp_tool_name, result_params
            )

        return await self._live_dispatch(
            action_id, tool_type, server_name, server_cfg, mcp_tool_name,
            params_to_use, result_params=result_params,
        )

    def _dispatch_one_dry(self, action: dict[str, Any]) -> dict[str, Any]:
        """Synchronous single-action dry-run (no process spawn, no asyncio)."""
        action_id: str = action.get("id", "unknown")
        tool_type: str = action.get("tool_type", "general_task")
        params: dict = action.get("tool_params", action.get("params", {}))

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

        params_to_use, sandbox_err = self._sandbox_params(
            server_name, mcp_tool_name, params
        )
        result_params = (
            _slack_result_params(params_to_use, params)
            if server_name == "slack" and mcp_tool_name == "slack_post_message"
            else params_to_use
        )
        if sandbox_err:
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, result_params,
                status="error", response=None, error=sandbox_err,
            )

        return self._dry_run_result(
            action_id, tool_type, server_name, mcp_tool_name, result_params
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
                "env": self._resolve_server_env(name),
                "transport": "stdio",
            }

        results: list[dict[str, Any]] = []
        client = MultiServerMCPClient(server_spec)
        try:
            tools = await client.get_tools()
            tools_by_name = {t.name: t for t in tools}

            for action in actions:
                action_id = action.get("id", "unknown")
                tool_type = action.get("tool_type", "general_task")
                params = action.get("tool_params", action.get("params", {}))
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

                params_to_use, sandbox_err = self._sandbox_params(
                    server_name, mcp_tool_name, params
                )
                result_params = (
                    _slack_result_params(params_to_use, params)
                    if server_name == "slack" and mcp_tool_name == "slack_post_message"
                    else params_to_use
                )
                if sandbox_err:
                    results.append(self._result(
                        action_id, tool_type, server_name, mcp_tool_name, result_params,
                        status="error", response=None, error=sandbox_err,
                    ))
                    continue

                tool = tools_by_name.get(mcp_tool_name)
                if tool is None:
                    available = list(tools_by_name.keys())
                    results.append(self._result(
                        action_id, tool_type, server_name, mcp_tool_name, result_params,
                        status="error", response=None,
                        error=f"Tool '{mcp_tool_name}' not found. Available: {available}",
                    ))
                    continue

                # Resolve Slack user ID (U...) to DM channel ID (D...) via conversations.open
                if server_name == "slack" and mcp_tool_name == "slack_post_message":
                    ch = params_to_use.get("channel_id", "")
                    if _is_slack_user_id(ch):
                        slack_env = self._resolve_server_env("slack")
                        token = slack_env.get("SLACK_BOT_TOKEN")
                        dm_channel = await _resolve_slack_user_to_dm_channel(token, ch)
                        if dm_channel:
                            params_to_use = {**params_to_use, "channel_id": dm_channel}
                            logger.debug("Resolved Slack user %s to DM channel %s", ch, dm_channel)
                        else:
                            results.append(self._result(
                                action_id, tool_type, server_name, mcp_tool_name, result_params,
                                status="error", response=None,
                                error="Could not open DM with user; check bot has im:write scope and user exists",
                            ))
                            continue

                try:
                    response = await tool.ainvoke(params_to_use)
                    # Log Slack response and treat ok:false as error
                    if server_name == "slack":
                        logger.info(
                            "Slack MCP response for action %s: %s",
                            action_id,
                            json.dumps(response, default=str),
                        )
                        slack_ok, slack_err = _parse_slack_mcp_response(response)
                        if not slack_ok and slack_err:
                            results.append(self._result(
                                action_id, tool_type, server_name, mcp_tool_name, result_params,
                                status="error", response=response, error=f"Slack API error: {slack_err}",
                            ))
                        else:
                            results.append(self._result(
                                action_id, tool_type, server_name, mcp_tool_name, result_params,
                                status="success", response=response, error=None,
                            ))
                    else:
                        results.append(self._result(
                            action_id, tool_type, server_name, mcp_tool_name, result_params,
                            status="success", response=response, error=None,
                        ))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("MCP dispatch failed for action %s", action_id)
                    results.append(self._result(
                        action_id, tool_type, server_name, mcp_tool_name, result_params,
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
        finally:
            if hasattr(client, "aclose") and callable(client.aclose):
                await client.aclose()
            elif hasattr(client, "close") and callable(client.close):
                close_fn = client.close
                if asyncio.iscoroutinefunction(close_fn):
                    await close_fn()
                else:
                    close_fn()

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
        result_params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """params = payload sent to MCP; result_params = payload in API response (includes display fields)."""
        out_params = result_params if result_params is not None else params
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient  # type: ignore
        except ImportError as exc:
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, out_params,
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
                "env": self._resolve_server_env(server_name),
                "transport": "stdio",
            }
        }

        client = MultiServerMCPClient(server_spec)
        try:
            tools = await client.get_tools()
            tool = next(
                (t for t in tools if t.name == mcp_tool_name), None
            )
            if tool is None:
                available = [t.name for t in tools]
                return self._result(
                    action_id, tool_type, server_name, mcp_tool_name, out_params,
                    status="error",
                    response=None,
                    error=f"Tool '{mcp_tool_name}' not found. Available: {available}",
                )

            # Resolve Slack user ID (U...) to DM channel ID (D...) via conversations.open
            params_to_invoke = params
            if server_name == "slack" and mcp_tool_name == "slack_post_message":
                ch = params.get("channel_id", "")
                if _is_slack_user_id(ch):
                    slack_env = self._resolve_server_env(server_name)
                    token = slack_env.get("SLACK_BOT_TOKEN")
                    dm_channel = await _resolve_slack_user_to_dm_channel(token, ch)
                    if dm_channel:
                        params_to_invoke = {**params, "channel_id": dm_channel}
                        logger.debug("Resolved Slack user %s to DM channel %s", ch, dm_channel)
                    else:
                        return self._result(
                            action_id, tool_type, server_name, mcp_tool_name, out_params,
                            status="error",
                            response=None,
                            error="Could not open DM with user; check bot has im:write scope and user exists",
                        )

            response = await tool.ainvoke(params_to_invoke)
            if server_name == "slack":
                logger.info(
                    "Slack MCP response for action %s: %s",
                    action_id,
                    json.dumps(response, default=str),
                )
                slack_ok, slack_err = _parse_slack_mcp_response(response)
                if not slack_ok and slack_err:
                    return self._result(
                        action_id, tool_type, server_name, mcp_tool_name, out_params,
                        status="error",
                        response=response,
                        error=f"Slack API error: {slack_err}",
                    )
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, out_params,
                status="success",
                response=response,
                error=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP dispatch failed for action %s", action_id)
            return self._result(
                action_id, tool_type, server_name, mcp_tool_name, out_params,
                status="error",
                response=None,
                error=str(exc),
            )
        finally:
            if hasattr(client, "aclose") and callable(client.aclose):
                await client.aclose()
            elif hasattr(client, "close") and callable(client.close):
                close_fn = client.close
                if asyncio.iscoroutinefunction(close_fn):
                    await close_fn()
                else:
                    close_fn()

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
