"""
Pure-data lookup tables for the Action Normalizer pipeline.

All constants here are rule-based — no imports from LLM or network code.
"""
from .models import ToolType


# ---------------------------------------------------------------------------
# VERB PHRASES
# Ordered longest-first so multi-word phrases match before their substrings.
# e.g. "circle back" must appear before "circle", "check on" before "check".
# ---------------------------------------------------------------------------

VERB_PHRASES: list[str] = sorted(
    [
        "circle back",
        "follow up",
        "follow through",
        "talk to",
        "speak with",
        "reach out",
        "check with",
        "check in",
        "check on",
        "look into",
        "set up",
        "clean up",
        "write up",
        "note down",
        "take care of",
        "deal with",
        "go over",
        "draft",
        "note",
        "own",
        "send",
        "email",
        "schedule",
        "book",
        "create",
        "fix",
        "investigate",
        "review",
        "check",
        "add",
        "notify",
        "inform",
        "tell",
        "document",
        "write",
        "track",
        "update",
        "test",
        "resolve",
        "implement",
        "deploy",
        "monitor",
        "configure",
        "refactor",
        "migrate",
        "analyze",
        "discuss",
        "prepare",
        "submit",
        "approve",
        "assign",
        "complete",
        "build",
        "research",
        "audit",
        "remove",
        "delete",
        "verify",
        "confirm",
        "coordinate",
        "ensure",
        "present",
        "push",
        "pull",
        "run",
        "execute",
        "close",
        "open",
        "start",
        "stop",
        "enable",
        "disable",
        "escalate",
        "triage",
        "unblock",
        "validate",
        "reproduce",
    ],
    key=len,
    reverse=True,
)


# ---------------------------------------------------------------------------
# VERB UPGRADES
# Maps colloquial / weak verb phrases → precise, tool-friendly verbs.
# Keys use spaces (not underscores) to match the output of _extract_verb_from_desc,
# which replaces underscores with spaces before lookup.
# ---------------------------------------------------------------------------

VERB_UPGRADES: dict[str, str] = {
    # Communication-flavored weak verbs → "notify"
    "talk to": "notify",
    "talk": "notify",
    "speak with": "notify",
    "speak": "notify",
    "tell": "notify",
    "let know": "notify",
    "reach out": "notify",
    "reach_out": "notify",
    "inform": "notify",
    "ping": "notify",
    "escalate": "notify",
    # Vague follow-up → "follow_up"
    "circle back": "follow_up",
    "circle_back": "follow_up",
    "follow through": "follow_up",
    "follow_through": "follow_up",
    # Investigation verbs
    "look into": "investigate",
    "look_into": "investigate",
    # Review / check verbs
    "check with": "notify",    # "check with [team]" is communication, not a work item
    "check_with": "notify",
    "check on": "review",
    "check_on": "review",
    "check in": "review",
    "check_in": "review",
    "check": "review",
    "go over": "review",
    "go_over": "review",
    # Resolution verbs
    "take care of": "resolve",
    "take_care_of": "resolve",
    "deal with": "resolve",
    "deal_with": "resolve",
    "handle": "resolve",
    # Strong verbs that need no upgrade — listed so they are preserved as-is
    # (not actually needed in the dict, but documenting intent)
}


# ---------------------------------------------------------------------------
# TOOL → VERB MAP
# Maps a normalized verb (may contain underscores) → ToolType.
# The classifier also accepts space-variants (e.g. "follow up" and "follow_up").
# ---------------------------------------------------------------------------

TOOL_VERB_MAP: dict[str, ToolType] = {
    # Email
    "send": ToolType.SEND_EMAIL,
    "draft": ToolType.SEND_EMAIL,
    "email": ToolType.SEND_EMAIL,
    # Notification / communication
    "notify": ToolType.SEND_NOTIFICATION,
    "tell": ToolType.SEND_NOTIFICATION,
    # "follow_up" intentionally omitted: context determines whether it's a
    # notification (follow up with client) or a task (follow up on tests).
    # The keyword scan in _classify_tool handles these cases correctly.
    # Calendar
    "schedule": ToolType.SET_CALENDAR,
    "book": ToolType.SET_CALENDAR,
    # Jira / task tracker
    "create": ToolType.CREATE_JIRA_TASK,
    "fix": ToolType.CREATE_JIRA_TASK,
    "investigate": ToolType.CREATE_JIRA_TASK,
    "review": ToolType.CREATE_JIRA_TASK,
    "track": ToolType.CREATE_JIRA_TASK,
    "resolve": ToolType.CREATE_JIRA_TASK,
    "implement": ToolType.CREATE_JIRA_TASK,
    "test": ToolType.CREATE_JIRA_TASK,
    "add": ToolType.CREATE_JIRA_TASK,
    "handle": ToolType.CREATE_JIRA_TASK,
    "verify": ToolType.CREATE_JIRA_TASK,
    "validate": ToolType.CREATE_JIRA_TASK,
    "reproduce": ToolType.CREATE_JIRA_TASK,
    "triage": ToolType.CREATE_JIRA_TASK,
    "unblock": ToolType.CREATE_JIRA_TASK,
    "audit": ToolType.CREATE_JIRA_TASK,
    "deploy": ToolType.CREATE_JIRA_TASK,
    "configure": ToolType.CREATE_JIRA_TASK,
    "refactor": ToolType.CREATE_JIRA_TASK,
    "migrate": ToolType.CREATE_JIRA_TASK,
    "build": ToolType.CREATE_JIRA_TASK,
    "complete": ToolType.CREATE_JIRA_TASK,
    "assign": ToolType.CREATE_JIRA_TASK,
    # Notion / documentation
    "document": ToolType.CREATE_NOTION_DOC,
    "write": ToolType.CREATE_NOTION_DOC,
    "write_up": ToolType.CREATE_NOTION_DOC,
    "write up": ToolType.CREATE_NOTION_DOC,
    "note": ToolType.CREATE_NOTION_DOC,
    "note_down": ToolType.CREATE_NOTION_DOC,
    "note down": ToolType.CREATE_NOTION_DOC,
    "own": ToolType.CREATE_NOTION_DOC,
    "prepare": ToolType.CREATE_NOTION_DOC,
}


# ---------------------------------------------------------------------------
# ACTION CATEGORY → TOOL TYPE (fallback when verb map has no match)
# Populated by the extractor LLM via the action_category field.
# ---------------------------------------------------------------------------

ACTION_CATEGORY_TOOL_MAP: dict[str, ToolType] = {
    "communication": ToolType.SEND_NOTIFICATION,
    "task": ToolType.CREATE_JIRA_TASK,
    "event": ToolType.SET_CALENDAR,
    "documentation": ToolType.CREATE_NOTION_DOC,
}


# ---------------------------------------------------------------------------
# COMPOUND SPLIT VERBS
# Action verbs whose simultaneous presence in a description (joined by "and"
# or similar) signals a potentially compound, splittable action item.
# ---------------------------------------------------------------------------

COMPOUND_SPLIT_VERBS: frozenset[str] = frozenset(
    {
        "investigate",
        "resolve",
        "fix",
        "review",
        "test",
        "implement",
        "document",
        "write",
        "deploy",
        "verify",
        "analyze",
        "audit",
        "create",
        "update",
        "notify",
        "inform",
        "schedule",
        "send",
        "draft",
    }
)


# ---------------------------------------------------------------------------
# DEDUPLICATION STOP WORDS
# High-frequency words excluded from Jaccard similarity calculation.
# ---------------------------------------------------------------------------

DEDUP_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a", "an", "the", "to", "for", "of", "and", "or", "in", "on", "at",
        "it", "that", "this", "is", "be", "with", "as", "by", "from", "up",
        "task", "item", "list", "add", "create", "note", "will", "should",
        "need", "make", "sure", "also", "about", "any", "all", "so", "do",
        "get", "out", "have", "has", "was", "are", "were", "been",
    }
)
