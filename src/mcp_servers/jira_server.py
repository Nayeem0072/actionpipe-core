"""
Jira MCP server (stdio transport).

Launched as a subprocess by MCPDispatcher when dry_run=False.
Credentials are injected via environment variables at launch time:

  JIRA_ACCESS_TOKEN  — Atlassian OAuth 2.0 access token for the user
  JIRA_CLOUD_ID      — Atlassian cloud ID (from user_tokens.meta["cloud_id"])

Exposes two tools:
  jira_create_issue  — Create a new Jira issue
  jira_update_issue  — Update fields on an existing Jira issue

Run directly for local testing:
  JIRA_ACCESS_TOKEN=... JIRA_CLOUD_ID=... python src/mcp_servers/jira_server.py
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FastMCP server instance
# ---------------------------------------------------------------------------

mcp = FastMCP("jira")

# ---------------------------------------------------------------------------
# Atlassian REST API helpers
# ---------------------------------------------------------------------------

_ATLASSIAN_BASE = "https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"

# Map simple priority words to Jira priority names
_PRIORITY_MAP: dict[str, str] = {
    "critical": "Highest",
    "urgent": "Highest",
    "blocker": "Highest",
    "high": "High",
    "medium": "Medium",
    "normal": "Medium",
    "low": "Low",
    "lowest": "Lowest",
    "minor": "Lowest",
}


def _get_credentials() -> tuple[str, str]:
    """Return (access_token, cloud_id) from env vars, raising if missing."""
    token = os.environ.get("JIRA_ACCESS_TOKEN", "").strip()
    cloud_id = os.environ.get("JIRA_CLOUD_ID", "").strip()
    if not token:
        raise ValueError("JIRA_ACCESS_TOKEN environment variable is not set")
    if not cloud_id:
        raise ValueError("JIRA_CLOUD_ID environment variable is not set")
    return token, cloud_id


def _base_url(cloud_id: str) -> str:
    return _ATLASSIAN_BASE.format(cloud_id=cloud_id)


def _auth_headers(access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _build_issue_fields(
    project_key: Optional[str],
    summary: Optional[str],
    description: Optional[str],
    assignee_account_id: Optional[str],
    priority: Optional[str],
    due_date: Optional[str],
    labels: Optional[list[str]],
    issue_type: Optional[str],
) -> dict:
    """Construct the Jira issue fields dict from optional parameters."""
    fields: dict = {}

    if project_key:
        fields["project"] = {"key": project_key.strip().upper()}

    if summary:
        fields["summary"] = summary.strip()

    if issue_type:
        fields["issuetype"] = {"name": issue_type}

    if description:
        # Atlassian Document Format (ADF) for v3 API
        fields["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description}],
                }
            ],
        }

    if assignee_account_id:
        fields["assignee"] = {"accountId": assignee_account_id.strip()}

    if priority:
        normalized = priority.strip().lower()
        jira_priority = _PRIORITY_MAP.get(normalized, priority.strip().capitalize())
        fields["priority"] = {"name": jira_priority}

    if due_date:
        fields["duedate"] = due_date.strip()

    if labels:
        cleaned = [str(lbl).replace(" ", "_") for lbl in labels if lbl]
        if cleaned:
            fields["labels"] = cleaned

    return fields


def _format_error(resp: httpx.Response) -> str:
    """Extract a readable error message from a Jira API error response."""
    try:
        data = resp.json()
        messages = []
        if "errorMessages" in data:
            messages.extend(data["errorMessages"])
        if "errors" in data:
            for field, msg in data["errors"].items():
                messages.append(f"{field}: {msg}")
        if messages:
            return "; ".join(messages)
    except Exception:
        pass
    return f"HTTP {resp.status_code}: {resp.text[:300]}"


# ---------------------------------------------------------------------------
# MCP tool: jira_create_issue
# ---------------------------------------------------------------------------


@mcp.tool()
async def jira_create_issue(
    project_key: str,
    summary: str,
    description: Optional[str] = None,
    assignee_account_id: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    labels: Optional[list[str]] = None,
    issue_type: str = "Task",
) -> str:
    """
    Create a new Jira issue.

    Args:
        project_key: The Jira project key (e.g. "PROJ", "ENG"). Required.
        summary: The issue title / summary. Required.
        description: Optional plain-text description for the issue body.
        assignee_account_id: Atlassian account ID of the assignee (not display name).
        priority: Priority string — one of: critical, urgent, blocker, high, medium,
                  normal, low, lowest, minor. Defaults to no explicit priority.
        due_date: Due date in YYYY-MM-DD format.
        labels: List of label strings (spaces replaced with underscores automatically).
        issue_type: Jira issue type name, e.g. "Task", "Bug", "Story". Default "Task".

    Returns:
        JSON string with the created issue key, id, and self URL on success,
        or an error message on failure.
    """
    access_token, cloud_id = _get_credentials()

    fields = _build_issue_fields(
        project_key=project_key,
        summary=summary,
        description=description,
        assignee_account_id=assignee_account_id,
        priority=priority,
        due_date=due_date,
        labels=labels,
        issue_type=issue_type,
    )

    payload = {"fields": fields}
    url = f"{_base_url(cloud_id)}/issue"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=_auth_headers(access_token), json=payload)

    if resp.status_code not in (200, 201):
        error_msg = _format_error(resp)
        logger.error("jira_create_issue failed (HTTP %s): %s", resp.status_code, error_msg)
        return json.dumps({"ok": False, "error": error_msg, "status_code": resp.status_code})

    data = resp.json()
    issue_key = data.get("key")
    issue_id = data.get("id")
    self_url = data.get("self")
    logger.info("Created Jira issue %s (id=%s)", issue_key, issue_id)
    return json.dumps({"ok": True, "key": issue_key, "id": issue_id, "self": self_url})


# ---------------------------------------------------------------------------
# MCP tool: jira_update_issue
# ---------------------------------------------------------------------------


@mcp.tool()
async def jira_update_issue(
    issue_key: str,
    summary: Optional[str] = None,
    description: Optional[str] = None,
    assignee_account_id: Optional[str] = None,
    priority: Optional[str] = None,
    due_date: Optional[str] = None,
    labels: Optional[list[str]] = None,
) -> str:
    """
    Update fields on an existing Jira issue.

    At least one optional field must be provided.

    Args:
        issue_key: The Jira issue key to update (e.g. "PROJ-123"). Required.
        summary: New summary / title for the issue.
        description: New plain-text description.
        assignee_account_id: Atlassian account ID of the new assignee.
        priority: New priority — one of: critical, urgent, blocker, high, medium,
                  normal, low, lowest, minor.
        due_date: New due date in YYYY-MM-DD format.
        labels: Replacement label list (replaces all existing labels).

    Returns:
        JSON string {"ok": true, "key": "PROJ-123"} on success or an error dict.
    """
    access_token, cloud_id = _get_credentials()

    fields = _build_issue_fields(
        project_key=None,
        summary=summary,
        description=description,
        assignee_account_id=assignee_account_id,
        priority=priority,
        due_date=due_date,
        labels=labels,
        issue_type=None,
    )

    if not fields:
        return json.dumps({"ok": False, "error": "No fields provided to update"})

    payload = {"fields": fields}
    url = f"{_base_url(cloud_id)}/issue/{issue_key.strip().upper()}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.put(url, headers=_auth_headers(access_token), json=payload)

    # Jira returns 204 No Content on successful update
    if resp.status_code not in (200, 204):
        error_msg = _format_error(resp)
        logger.error("jira_update_issue failed (HTTP %s): %s", resp.status_code, error_msg)
        return json.dumps({"ok": False, "error": error_msg, "status_code": resp.status_code})

    logger.info("Updated Jira issue %s", issue_key)
    return json.dumps({"ok": True, "key": issue_key.strip().upper()})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
