"""Contact resolver — enriches NormalizedAction tool_params using the relation graph."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import Connection, Person, RelationGraph

logger = logging.getLogger(__name__)

_DEFAULT_CONTACTS_PATH = Path(__file__).parent / "contacts.json"

# region agent log
_DEBUG_LOG_PATH = Path(__file__).resolve().parents[2] / ".cursor" / "debug.log"


def _write_debug_log(message: str, data: dict[str, Any], hypothesis_id: str) -> None:
    """
    Append a single NDJSON debug log line to the shared debug file.
    Kept intentionally tiny for runtime debugging.
    """
    try:
        ts_ms = int(time.time() * 1000)
        payload = {
            "id": f"log_{ts_ms}",
            "timestamp": ts_ms,
            "location": "src/relation_graph/resolver.py",
            "message": message,
            "data": data,
            "runId": data.get("runId") or "unknown",
            "hypothesisId": hypothesis_id,
        }
        _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str) + "\n")
    except Exception:
        # Logging must never break the main flow
        return


# endregion


# ---------------------------------------------------------------------------
# LLM response schema
# ---------------------------------------------------------------------------

class ConnectionResolution(BaseModel):
    """LLM decision: which connection to use for an action, with confidence."""

    connection_key: Optional[str] = Field(
        default=None,
        description=(
            "The key of the most relevant connection from the person's connections dict. "
            "Return null if no connection clearly applies."
        ),
    )
    confidence: float = Field(
        description="Confidence that this is the right connection (0.0 = no idea, 1.0 = certain).",
    )
    reasoning: str = Field(
        description="One sentence explaining why this connection was chosen (or why none applies).",
    )


# ---------------------------------------------------------------------------
# LLM factory (mirrors the pattern used in action_normalizer/nodes.py)
# ---------------------------------------------------------------------------

def _create_resolver_llm():
    """Return an LLM instance configured via the project's llm_config."""
    from ..action_extractor.llm_config import LOCAL_EXTRACTOR_CONFIG
    from ..action_extractor.nodes import create_llm

    return create_llm(LOCAL_EXTRACTOR_CONFIG)


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

_RESOLUTION_PROMPT = """\
You are a contact routing assistant for a meeting action item system.

Given an action item and a person's available connections, decide which single connection \
is the most relevant recipient or participant. Return null if none clearly applies.

Action description: {description}
Assignee: {assignee}
Action type: {tool_type}
Topic tags: {topic_tags}

{assignee}'s available connections:
{connections_summary}

Rules:
- For a calendar/scheduling action, prefer the group or team whose work relates to the event topic.
- For a notification/email, prefer the department or external party the action is directed toward.
- If the description mentions a specific team, department, or person by name, prefer that connection.
- Assign confidence < 0.5 when the action is ambiguous or no connection fits well.
"""


def _summarise_connections(person: Person) -> str:
    """Build a human-readable summary of a person's connections for the LLM prompt."""
    if not person.connections:
        return "  (no named connections)"
    lines = []
    for key, conn in person.connections.items():
        parts = [f"  [{key}]"]
        if conn.members:
            names = ", ".join(
                f"{m.name} ({m.email})" if m.email else m.name
                for m in conn.members
            )
            parts.append(f"group — members: {names}")
        elif conn.slack_channel and conn.email:
            parts.append(f"slack {conn.slack_channel}, email {conn.email}")
        elif conn.slack_channel:
            parts.append(f"slack {conn.slack_channel}")
        elif conn.email:
            parts.append(f"email {conn.email}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ContactResolver
# ---------------------------------------------------------------------------

class ContactResolver:
    """
    Loads the relation graph and enriches tool_params for each NormalizedAction.

    Connection resolution uses an LLM call that reads the action description,
    tool type, and topic tags, then picks the most relevant connection from the
    assignee's contacts.json entry. The decision includes a confidence score
    and a one-sentence reasoning string, both stored on the enriched action under
    the key ``connection_resolution``.

    Resolution priority for each field:
      1. LLM-chosen connection (based on description + topic_tags)
      2. Person's own contact details (for Jira user, Notion workspace)
      3. Leave field unchanged if nothing is found
    """

    def __init__(
        self,
        contacts_path: Optional[Path] = None,
        contacts_graph: Optional[dict] = None,
        llm=None,
    ) -> None:
        if contacts_graph is not None:
            self._graph = RelationGraph.model_validate(contacts_graph)
        else:
            path = contacts_path or _DEFAULT_CONTACTS_PATH
            raw = json.loads(path.read_text(encoding="utf-8"))
            self._graph = RelationGraph.model_validate(raw)
        self._llm = llm  # injected in tests; lazy-loaded on first use otherwise
        self._structured_llm = None

    def _get_structured_llm(self):
        if self._structured_llm is None:
            base = self._llm or _create_resolver_llm()
            self._structured_llm = base.with_structured_output(ConnectionResolution)
        return self._structured_llm

    # ------------------------------------------------------------------
    # Public resolution helpers
    # ------------------------------------------------------------------

    def get_person(self, name: Optional[str]) -> Optional[Person]:
        if not name:
            return None
        return self._graph.people.get(name)

    def resolve_email(self, name: Optional[str]) -> Optional[str]:
        """Return the person's own email address."""
        person = self.get_person(name)
        return person.email if person else None

    def resolve_slack(
        self, name: Optional[str], connection_key: Optional[str] = None
    ) -> Optional[str]:
        """
        Return a Slack handle or channel.

        If connection_key is provided and the person has that connection, return
        the connection's slack_channel. Otherwise return the person's own handle.
        """
        person = self.get_person(name)
        if not person:
            return None
        if connection_key and connection_key in person.connections:
            conn = person.connections[connection_key]
            if conn.slack_channel:
                return conn.slack_channel
        return person.slack_handle

    def resolve_participants(
        self, connection: Optional[Connection]
    ) -> list[dict[str, str]]:
        """Return a list of {name, email} from a resolved connection's member list."""
        if connection and connection.members:
            return [
                {"name": m.name, "email": m.email or ""}
                for m in connection.members
            ]
        return []

    def resolve_jira_user(self, name: Optional[str]) -> Optional[str]:
        person = self.get_person(name)
        if person and person.jira_user:
            return person.jira_user
        return name

    def resolve_notion_workspace(self, name: Optional[str]) -> Optional[str]:
        person = self.get_person(name)
        return person.notion_workspace if person else None

    # ------------------------------------------------------------------
    # Main enrichment entry point
    # ------------------------------------------------------------------

    def enrich_tool_params(self, action: dict[str, Any]) -> dict[str, Any]:
        """
        Return a copy of action with tool_params enriched using the relation graph.

        Adds a top-level ``connection_resolution`` key:
          {connection_key, confidence, reasoning}

        The original action dict is not mutated.
        """
        import copy

        action = copy.deepcopy(action)
        tool_type: str = action.get("tool_type", "")
        params: dict = action.get("tool_params", {})
        assignee: Optional[str] = action.get("assignee")
        description: str = action.get("description", "")
        topic_tags: list[str] = action.get("topic_tags", [])

        resolution, connection = self._resolve_connection(
            assignee=assignee,
            description=description,
            tool_type=tool_type,
            topic_tags=topic_tags,
        )

        # region agent log
        _write_debug_log(
            "contact_resolution",
            {
                "runId": action.get("run_id") or action.get("runId") or "unknown",
                "actionId": action.get("id"),
                "toolType": tool_type,
                "assignee": assignee,
                "connectionKey": resolution.connection_key,
                "hasConnection": bool(connection),
                "topicTags": topic_tags,
            },
            hypothesis_id="H1",
        )
        # endregion

        action["connection_resolution"] = {
            "connection_key": resolution.connection_key,
            "confidence": resolution.confidence,
            "reasoning": resolution.reasoning,
        }

        if tool_type == "send_email":
            params = self._enrich_email(params, assignee, connection, resolution.connection_key)

        elif tool_type == "set_calendar":
            params = self._enrich_calendar(params, assignee, connection)

        elif tool_type == "send_notification":
            params = self._enrich_notification(
                params,
                assignee,
                connection,
                resolution.connection_key,
            )

        elif tool_type == "create_jira_task":
            params = self._enrich_jira(params, assignee)

        elif tool_type == "create_notion_doc":
            params = self._enrich_notion(params, assignee)

        action["tool_params"] = params
        return action

    # ------------------------------------------------------------------
    # LLM connection resolution
    # ------------------------------------------------------------------

    def _resolve_connection(
        self,
        assignee: Optional[str],
        description: str,
        tool_type: str,
        topic_tags: list[str],
    ) -> tuple[ConnectionResolution, Optional[Connection]]:
        """
        Call the LLM to decide which of the assignee's connections is most relevant.

        Returns (ConnectionResolution, Connection | None).
        """
        person = self.get_person(assignee)
        if not person or not person.connections:
            return (
                ConnectionResolution(
                    connection_key=None,
                    confidence=0.0,
                    reasoning="Assignee has no named connections.",
                ),
                None,
            )

        prompt = _RESOLUTION_PROMPT.format(
            description=description,
            assignee=assignee or "unknown",
            tool_type=tool_type,
            topic_tags=", ".join(topic_tags) if topic_tags else "none",
            connections_summary=_summarise_connections(person),
        )

        try:
            resolution: ConnectionResolution = self._get_structured_llm().invoke(prompt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LLM connection resolution failed for action (assignee=%s): %s",
                assignee,
                exc,
            )
            return (
                ConnectionResolution(
                    connection_key=None,
                    confidence=0.0,
                    reasoning=f"LLM call failed: {exc}",
                ),
                None,
            )

        logger.debug(
            "Connection resolved for %s: key=%s confidence=%.2f — %s",
            assignee,
            resolution.connection_key,
            resolution.confidence,
            resolution.reasoning,
        )

        connection = (
            person.connections.get(resolution.connection_key)
            if resolution.connection_key
            else None
        )
        return resolution, connection

    # ------------------------------------------------------------------
    # Per-tool enrichment helpers
    # ------------------------------------------------------------------

    def _enrich_email(
        self,
        params: dict,
        assignee: Optional[str],
        connection: Optional[Connection],
        connection_key: Optional[str] = None,
    ) -> dict:
        if connection and connection.email:
            params["to"] = connection.email
            params["to_display_name"] = connection_key or assignee or connection.email
        elif params.get("to") in (None, "", assignee):
            email = self.resolve_email(assignee)
            if email:
                params["to"] = email
                params["to_display_name"] = assignee or email
        # Fallback: recipient set but no display name (e.g. from normalizer)
        if params.get("to") and "to_display_name" not in params:
            params["to_display_name"] = (
                assignee or connection_key or params["to"].split("@")[0]
            )
        return params

    def _enrich_calendar(
        self,
        params: dict,
        assignee: Optional[str],
        connection: Optional[Connection],
    ) -> dict:
        if not params.get("participants"):
            participants = self.resolve_participants(connection)
            if participants:
                params["participants"] = participants
            organiser_email = self.resolve_email(assignee)
            if organiser_email:
                organiser = {"name": assignee or "", "email": organiser_email}
                existing_emails = {p["email"] for p in params.get("participants", [])}
                if organiser_email not in existing_emails:
                    params.setdefault("participants", []).append(organiser)
        if params.get("time") is None:
            import re
            event_name: str = params.get("event_name", "")
            match = re.search(r"\b(\d{1,2})\s*(am|pm)\b", event_name, re.IGNORECASE)
            if match:
                hour, meridiem = match.group(1), match.group(2).lower()
                params["time"] = f"{hour}:00 {meridiem.upper()}"
        return params

    def _enrich_notification(
        self,
        params: dict,
        assignee: Optional[str],
        connection: Optional[Connection],
        connection_key: Optional[str] = None,
    ) -> dict:
        current_recipient = params.get("recipient") or ""
        recipient_is_valid = (
            len(current_recipient) > 3
            and not current_recipient.isalpha()
            or current_recipient.startswith(("#", "@"))
            or "@" in current_recipient
        )

        # region agent log
        _write_debug_log(
            "notification_before_enrich",
            {
                "runId": params.get("run_id") or params.get("runId") or "unknown",
                "assignee": assignee,
                "currentRecipient": current_recipient,
                "recipientIsValid": recipient_is_valid,
                "hasConnection": bool(connection),
                "connectionSlackChannel": getattr(connection, "slack_channel", None) if connection else None,
            },
            hypothesis_id="H2",
        )
        # endregion

        # Prefer org contacts (DB / contacts graph) over the LLM's free-text recipient.
        # If a connection exists, always use its Slack channel or email, regardless of
        # whether the current recipient string "looks valid".
        if connection:
            if connection.slack_channel:
                params["recipient"] = connection.slack_channel
                params["channel"] = "slack"
                # Use the connection key (team/department name) for display when available.
                params["recipient_display_name"] = connection_key or connection.slack_channel.lstrip("#")
            elif connection.email:
                params["recipient"] = connection.email
                params["channel"] = "email"
                params["recipient_display_name"] = connection_key or assignee or connection.email
        elif not recipient_is_valid:
            slack = self.resolve_slack(assignee)
            if slack:
                params["recipient"] = slack
                # When recipient is a Slack user ID (e.g. U0AKYDAC3U4), use assignee name for display
                if slack.startswith("U") and len(slack) >= 9 and assignee:
                    params["recipient_display_name"] = assignee
                elif slack.startswith("#"):
                    params["recipient_display_name"] = slack.lstrip("#")
                else:
                    params["recipient_display_name"] = assignee or slack
        # Fallback: if recipient is set but no display name yet (e.g. valid channel from extractor), add one
        if params.get("recipient") and "recipient_display_name" not in params:
            r = params["recipient"]
            if r.startswith("#"):
                params["recipient_display_name"] = r.lstrip("#")
            elif r.startswith("U") and len(r) >= 9:
                params["recipient_display_name"] = assignee or r
            else:
                params["recipient_display_name"] = assignee or r
        # Ensure frontend always has a display name (e.g. when recipient couldn't be resolved)
        if "recipient_display_name" not in params:
            hint = (params.get("message_hint") or "")[:50].strip()
            params["recipient_display_name"] = (
                assignee or params.get("recipient") or hint or "Unknown"
            )

        # region agent log
        _write_debug_log(
            "notification_after_enrich",
            {
                "runId": params.get("run_id") or params.get("runId") or "unknown",
                "assignee": assignee,
                "finalRecipient": params.get("recipient"),
                "channel": params.get("channel"),
                "recipientDisplayName": params.get("recipient_display_name"),
                "messageHint": (params.get("message_hint") or "")[:80],
            },
            hypothesis_id="H3",
        )
        # endregion

        return params

    def _enrich_jira(self, params: dict, assignee: Optional[str]) -> dict:
        jira_user = self.resolve_jira_user(assignee)
        if jira_user:
            params["assignee"] = jira_user
            # Keep a display name for the frontend when assignee is an ID (e.g. Jira account id)
            if assignee:
                params["assignee_display_name"] = assignee
        return params

    def _enrich_notion(self, params: dict, assignee: Optional[str]) -> dict:
        workspace = self.resolve_notion_workspace(assignee)
        if workspace:
            params["workspace"] = workspace
        return params
