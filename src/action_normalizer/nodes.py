"""
Nodes for the Action Normalizer LangGraph pipeline.

Pipeline order:
  deadline_normalizer → verb_enricher → action_splitter → deduplicator → tool_classifier

Rule-based processing dominates; LLM is only invoked for genuinely ambiguous cases.
"""
from __future__ import annotations

import calendar
import logging
import re
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel as PydanticBaseModel

from .data import (
    ACTION_CATEGORY_TOOL_MAP,
    COMPOUND_SPLIT_VERBS,
    DEDUP_STOP_WORDS,
    TOOL_VERB_MAP,
    VERB_PHRASES,
    VERB_UPGRADES,
)
from .models import NormalizedAction, ToolType
from .state import NormalizerState
from ..action_extractor.llm_config import LOCAL_EXTRACTOR_CONFIG
from ..action_extractor.nodes import create_llm

try:
    from dateutil import parser as _dateutil_parser

    _HAS_DATEUTIL = True
except ImportError:
    _HAS_DATEUTIL = False

logger = logging.getLogger(__name__)


# ===========================================================================
# SHARED HELPERS
# ===========================================================================


def _normalize_deadline(raw: str | None, meeting_date: date) -> str | None:
    """
    Convert a free-text deadline string to ISO 8601 (YYYY-MM-DD) or None.

    Processing order:
      1. Null synonyms → None
      2. Post-meeting relative phrases → meeting_date (today)
      3. End-of-day / ASAP / today → meeting_date
      4. Tomorrow → meeting_date + 1
      5. This week / end of week → Friday of current week
      6. Next week → Monday of next week
      7. End of month → last day of current month
      8. Next month → 1st of next month
      9. dateutil parsing for explicit dates like "March 10", "March 10 at 2 pm"
      10. Manual month-name fallback when dateutil is not installed
    """
    if raw is None:
        return None

    s = raw.strip().lower()

    # 1. Null synonyms
    _NULL_SYNONYMS = {
        "later", "tbd", "undefined", "unknown", "none", "n/a",
        "no deadline", "sometime", "eventually", "when possible",
        "flexible", "no rush", "unspecified", "to be determined",
    }
    if s in _NULL_SYNONYMS:
        return None

    # 2. Post-meeting → today
    if re.search(
        r"\b(after\s+(the\s+)?meeting|post.?meeting|end\s+of\s+(the\s+)?meeting|after\s+this)\b",
        s,
    ):
        return meeting_date.isoformat()

    # 3. End of day / today / ASAP
    if re.search(
        r"\b(end\s+of\s+(the\s+)?day|eod|today|asap|as\s+soon\s+as\s+possible|immediately|right\s+away)\b",
        s,
    ):
        return meeting_date.isoformat()

    # 4. Tomorrow
    if re.search(r"\btomorrow\b", s):
        return (meeting_date + timedelta(days=1)).isoformat()

    # 5. End of week / this week / EOW → Friday
    if re.search(
        r"\b(end\s+of\s+(the\s+)?week|this\s+week|by\s+friday|by\s+end\s+of\s+week|eow)\b", s
    ):
        days_until_friday = (4 - meeting_date.weekday()) % 7 or 7
        return (meeting_date + timedelta(days=days_until_friday)).isoformat()

    # 6. Next week → next Monday
    if re.search(r"\bnext\s+week\b", s):
        days_ahead = 7 - meeting_date.weekday()
        if days_ahead == 0:
            days_ahead = 7
        return (meeting_date + timedelta(days=days_ahead)).isoformat()

    # 7. End of month → last day of current month
    if re.search(r"\bend\s+of\s+(the\s+)?month\b", s):
        last_day = calendar.monthrange(meeting_date.year, meeting_date.month)[1]
        return date(meeting_date.year, meeting_date.month, last_day).isoformat()

    # 8. Next month → 1st of next month
    if re.search(r"\bnext\s+month\b", s):
        if meeting_date.month == 12:
            return date(meeting_date.year + 1, 1, 1).isoformat()
        return date(meeting_date.year, meeting_date.month + 1, 1).isoformat()

    # 9. dateutil parsing (handles "March 10", "March 10 at 2 pm", "10/3", etc.)
    if _HAS_DATEUTIL:
        try:
            parsed = _dateutil_parser.parse(
                raw,
                default=date(meeting_date.year, meeting_date.month, meeting_date.day),
                dayfirst=False,
            )
            parsed_date = parsed.date() if hasattr(parsed, "date") else parsed
            if isinstance(parsed_date, date):
                if parsed_date < meeting_date:
                    parsed_date = parsed_date.replace(year=parsed_date.year + 1)
                return parsed_date.isoformat()
        except (ValueError, OverflowError, TypeError):
            pass

    # 10. Manual month-name fallback
    _MONTH_NAMES = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    for month_name, month_num in _MONTH_NAMES.items():
        m = re.search(rf"\b{month_name}\s+(\d{{1,2}})\b", s)
        if m:
            try:
                d = date(meeting_date.year, month_num, int(m.group(1)))
                if d < meeting_date:
                    d = d.replace(year=d.year + 1)
                return d.isoformat()
            except ValueError:
                pass

    logger.warning("DeadlineNormalizer: Could not normalize deadline: %r", raw)
    return None


def _extract_verb_from_desc(description: str) -> str | None:
    """
    Extract the primary action verb phrase from a description.

    Strategy (in order):
      1. Match VERB_PHRASES at the start (longest-first, so "circle back" beats "circle").
      2. Detect "Name will/to/needs to [verb]" patterns — skip past the name to find the verb.
      3. Fall back to the first word only when it is lowercase in the original (i.e. it was
         written as a verb, not a capitalised proper noun at the start of a sentence).
    """
    desc_lower = description.lower().strip()

    # 1. Verb phrase at start of description
    for phrase in VERB_PHRASES:
        if desc_lower.startswith(phrase):
            return phrase.replace(" ", "_")

    # 2. Name-prefix pattern: "[Name] will/to/needs to/should/has to/must [verb ...]"
    #    Matches descriptions like "John will talk to finance ..." or "Priya to create ..."
    name_prefix = re.match(
        r"^[A-Z][a-z]+\s+(?:will|to|needs?\s+to|should|has\s+to|must|can|is\s+going\s+to)\s+(.+)",
        description,
    )
    if name_prefix:
        rest = name_prefix.group(1).lower().strip()
        for phrase in VERB_PHRASES:
            if rest.startswith(phrase):
                return phrase.replace(" ", "_")
        # First word of the remainder
        first = rest.split()[0] if rest else None
        if first and re.match(r"^[a-z]+$", first):
            return first

    # 3. Fallback: first word — only use it when it was originally lowercase
    #    (capitalised first words at sentence start may be proper names, not verbs).
    first_word_original = description.split()[0] if description else ""
    if first_word_original and first_word_original[0].islower():
        return first_word_original.lower()

    return None


def _jaccard_similarity(text1: str, text2: str) -> float:
    """Token-level Jaccard similarity, ignoring DEDUP_STOP_WORDS."""
    w1 = {w for w in text1.lower().split() if w not in DEDUP_STOP_WORDS}
    w2 = {w for w in text2.lower().split() if w not in DEDUP_STOP_WORDS}
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


def _is_compound(description: str) -> bool:
    """
    Return True when the description likely contains 2+ distinct atomic actions.

    Compound detection requires BOTH:
      (a) A conjunction keyword ("and", "as well as", etc.)
      (b) At least 2 distinct action verbs from COMPOUND_SPLIT_VERBS
    """
    desc_lower = description.lower()

    if not re.search(
        r"\b(and|as\s+well\s+as|additionally|also\s+(?:need|should))\b", desc_lower
    ):
        return False

    found_verbs = {
        verb
        for verb in COMPOUND_SPLIT_VERBS
        if re.search(r"\b" + re.escape(verb.replace("_", " ")) + r"\b", desc_lower)
    }
    return len(found_verbs) >= 2


def _classify_tool(action: NormalizedAction) -> ToolType:
    """
    Rule-based tool classification.

    Priority:
      1. Verb → TOOL_VERB_MAP (most reliable signal)
      2. action_category from extractor → ACTION_CATEGORY_TOOL_MAP
      3. Keyword scan of description (secondary patterns)
      4. Falls back to GENERAL_TASK (triggers LLM in tool_classifier_node)
    """
    verb = action.verb or ""
    desc_lower = action.description.lower()

    # 1. Verb-based (try both underscore and space variants)
    for v in (verb, verb.replace("_", " ")):
        if v in TOOL_VERB_MAP:
            return TOOL_VERB_MAP[v]

    # 2. action_category hint from the extractor
    if action.action_category and action.action_category in ACTION_CATEGORY_TOOL_MAP:
        return ACTION_CATEGORY_TOOL_MAP[action.action_category]

    # 3. Keyword scan
    if re.search(r"\b(email|e-mail|draft\s+.*email|send\s+.*email)\b", desc_lower):
        return ToolType.SEND_EMAIL
    if re.search(r"\b(schedule|book|calendar|meeting|session|standup|sync|event)\b", desc_lower):
        return ToolType.SET_CALENDAR
    if re.search(
        r"\b(jira|ticket|task|bug|issue|fix|investigate|resolve|implement|"
        r"deploy|test|track|sprint|backlog)\b",
        desc_lower,
    ):
        return ToolType.CREATE_JIRA_TASK
    if re.search(
        r"\b(notion|document|doc|wiki|runbook|write.?up|write\s+up|notes?|"
        r"documentation)\b",
        desc_lower,
    ):
        return ToolType.CREATE_NOTION_DOC
    if re.search(
        r"\b(notify|inform|tell|slack|message|ping|alert|escalate|"
        r"update\s+(?:the\s+)?team|let\s+.*know)\b",
        desc_lower,
    ):
        return ToolType.SEND_NOTIFICATION

    return ToolType.GENERAL_TASK


def _extract_tool_params(action: NormalizedAction) -> dict:
    """Extract tool-specific parameters from the action description using regex."""
    desc = action.description
    tt = action.tool_type

    if tt == ToolType.SEND_EMAIL:
        params: Dict[str, Any] = {
            "to": None,
            "subject_hint": desc[:60],
            "body_hint": desc,
        }
        # "to [client/person/team]" pattern
        to_m = re.search(
            r"\bto\s+(client|customer|team|finance|management|stakeholders|"
            r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b",
            desc,
        )
        if to_m:
            params["to"] = to_m.group(1)
        elif action.assignee:
            params["to"] = action.assignee
        return params

    elif tt == ToolType.CREATE_JIRA_TASK:
        priority = (
            "high" if action.confidence >= 0.9
            else "medium" if action.confidence >= 0.7
            else "low"
        )
        if re.search(r"\b(urgent|critical|blocker|asap|p0|p1)\b", desc.lower()):
            priority = "high"
        return {
            "title": desc[:100],
            "assignee": action.assignee,
            "priority": priority,
            "due_date": action.normalized_deadline,
            "labels": action.topic_tags,
        }

    elif tt == ToolType.SET_CALENDAR:
        # Strip leading scheduling verb for a cleaner event name
        event_name = re.sub(
            r"^(schedule|book|set\s+up|organize|arrange|plan)\s+(a\s+|an\s+)?",
            "",
            desc,
            flags=re.IGNORECASE,
        ).strip().capitalize()
        params = {
            "event_name": event_name or desc[:80],
            "date": action.normalized_deadline,
            "time": None,
            "participants": [],
        }
        time_m = re.search(r"\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", desc, re.IGNORECASE)
        if time_m:
            params["time"] = time_m.group(1).strip()
        # Collect capitalized words that look like first names (exclude known non-names)
        _NON_NAMES = {
            "March", "April", "June", "July", "August", "Monday", "Tuesday",
            "Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Sprint",
            "Schedule", "Book", "Organize", "Plan", "Arrange", "Session",
            "Meeting", "Event", "Bug", "Bash", "Review", "Demo", "Sync",
        }
        names = [n for n in re.findall(r"\b([A-Z][a-z]{2,})\b", desc) if n not in _NON_NAMES]
        params["participants"] = list(dict.fromkeys(names))  # deduplicate, preserve order
        return params

    elif tt == ToolType.SEND_NOTIFICATION:
        params = {"recipient": None, "channel": "slack", "message_hint": desc}
        # "talk to / notify / inform [X]" → extract recipient
        rec_m = re.search(
            r"(?:talk\s+to|notify|inform|tell|reach\s+out\s+to|contact|escalate\s+to)\s+"
            r"(?:the\s+|a\s+|an\s+)?([A-Za-z]+(?:\s+(?:team|department|group))?)",
            desc,
            re.IGNORECASE,
        )
        if rec_m:
            params["recipient"] = rec_m.group(1).strip()
        else:
            # "to [X] to inform / about"
            to_m = re.search(
                r"\bto\s+([A-Za-z]+)\s+(?:to\s+inform|about|regarding|that|of)\b",
                desc,
                re.IGNORECASE,
            )
            if to_m:
                params["recipient"] = to_m.group(1)
        return params

    elif tt == ToolType.CREATE_NOTION_DOC:
        title = re.sub(
            r"^(document|write\s+up|write|record|note|prepare\s+(?:a\s+)?)\s+(a\s+|an\s+)?",
            "",
            desc,
            flags=re.IGNORECASE,
        ).strip()
        return {
            "page_title": title[:60].capitalize() if title else desc[:60],
            "content_hint": desc,
            "template": "meeting-action",
        }

    # GENERAL_TASK fallback
    return {
        "title": desc[:100],
        "assignee": action.assignee,
        "due_date": action.normalized_deadline,
    }


# ===========================================================================
# NODE 1 — DEADLINE NORMALIZER
# ===========================================================================


def deadline_normalizer_node(state: NormalizerState) -> NormalizerState:
    """
    Convert raw action dicts → NormalizedAction objects and normalize all deadlines.

    This node is always the first node in the pipeline and is entirely rule-based.
    It also initialises tool_type to GENERAL_TASK (placeholder for tool_classifier).
    """
    raw = state.get("raw_actions", [])
    meeting_date_str = state.get("meeting_date")
    meeting_date = date.fromisoformat(meeting_date_str) if meeting_date_str else date.today()

    working: List[NormalizedAction] = []
    for item in raw:
        d: dict = item.model_dump() if hasattr(item, "model_dump") else dict(item)

        raw_dl = d.get("deadline")
        norm_dl = _normalize_deadline(raw_dl, meeting_date)

        mw_raw = d.get("meeting_window")
        meeting_window: Optional[tuple[int, int]] = None
        if isinstance(mw_raw, (list, tuple)) and len(mw_raw) == 2:
            meeting_window = (int(mw_raw[0]), int(mw_raw[1]))

        action = NormalizedAction(
            id=str(uuid.uuid4())[:8],
            description=d.get("description", ""),
            assignee=d.get("assignee"),
            raw_deadline=raw_dl,
            normalized_deadline=norm_dl,
            speaker=d.get("speaker", ""),
            verb=d.get("verb", "do"),
            confidence=float(d.get("confidence") or 0.5),
            tool_type=ToolType.GENERAL_TASK,
            tool_params={},
            source_spans=list(d.get("source_spans") or []),
            meeting_window=meeting_window,
            action_category=d.get("action_category"),
            topic_tags=list(d.get("topic_tags") or []),
        )
        working.append(action)
        logger.debug(
            "DeadlineNormalizer: %r  raw=%r → %r",
            action.description[:50],
            raw_dl,
            norm_dl,
        )

    logger.info("DeadlineNormalizer: %d actions initialised, deadlines normalised", len(working))
    return {**state, "working_actions": working}


# ===========================================================================
# NODE 2 — VERB ENRICHER
# ===========================================================================


class _VerbEnrichmentResult(PydanticBaseModel):
    verbs: List[str]


def verb_enricher_node(
    state: NormalizerState,
    config: Optional[RunnableConfig] = None,
) -> NormalizerState:
    """
    Extract meaningful verbs from descriptions and apply the upgrade dictionary.

    Steps:
      1. Extract verb from description start via VERB_PHRASES (rule-based).
      2. Apply VERB_UPGRADES dictionary (e.g. "talk to" → "notify").
      3. For the rare cases where no verb can be determined, batch-call the LLM.
    """
    working = state.get("working_actions", [])
    updated: List[NormalizedAction] = []
    llm_indices: List[int] = []

    for action in working:
        extracted = _extract_verb_from_desc(action.description)
        raw_verb = extracted or action.verb or "do"

        # Lookup with both underscore and space variants
        lookup = raw_verb.replace("_", " ")
        upgraded = VERB_UPGRADES.get(lookup, VERB_UPGRADES.get(raw_verb, raw_verb))

        if upgraded == "do":
            llm_indices.append(len(updated))

        updated.append(action.model_copy(update={"verb": upgraded}))

    # LLM fallback — only for actions still carrying the generic "do" verb
    if llm_indices:
        logger.info("VerbEnricher: %d actions need LLM verb extraction", len(llm_indices))
        descriptions = [updated[i].description for i in llm_indices]
        try:
            llm = create_llm(LOCAL_EXTRACTOR_CONFIG)
            structured_llm = llm.with_structured_output(_VerbEnrichmentResult)
            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "Extract the primary action verb for each action item.\n"
                        "Return one concise lowercase verb or short verb phrase per item "
                        "(e.g. 'send', 'review', 'investigate', 'notify', 'schedule').\n"
                        "Choose verbs that best indicate which tool should execute the action.\n"
                        'Return JSON: {{"verbs": ["verb1", "verb2", ...]}} in the same order as input.',
                    ),
                    ("human", "Action items:\n{descriptions}"),
                ]
            )
            chain = prompt | structured_llm
            desc_text = "\n".join(f"{i + 1}. {d}" for i, d in enumerate(descriptions))
            result = chain.invoke({"descriptions": desc_text}, config=config)

            if result.verbs and len(result.verbs) == len(llm_indices):
                for orig_idx, llm_verb in zip(llm_indices, result.verbs):
                    v = llm_verb.strip().lower().replace(" ", "_")
                    lookup = v.replace("_", " ")
                    upgraded_llm = VERB_UPGRADES.get(lookup, VERB_UPGRADES.get(v, v))
                    updated[orig_idx] = updated[orig_idx].model_copy(
                        update={"verb": upgraded_llm}
                    )
        except Exception as exc:
            logger.warning("VerbEnricher: LLM fallback failed: %s", exc)

    logger.info("VerbEnricher: %d actions verb-enriched", len(updated))
    return {**state, "working_actions": updated}


# ===========================================================================
# NODE 3 — ACTION SPLITTER
# ===========================================================================


class _SplitDecision(PydanticBaseModel):
    should_split: bool
    splits: List[str]


def _llm_split_action(
    action: NormalizedAction,
    config: Optional[RunnableConfig] = None,
) -> List[NormalizedAction]:
    """
    Ask the LLM whether an action should be split and, if so, return the atomic pieces.

    Falls back to [action] (unchanged) if the LLM errors or decides not to split.
    """
    try:
        llm = create_llm(LOCAL_EXTRACTOR_CONFIG)
        structured_llm = llm.with_structured_output(_SplitDecision)
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "Decide whether an action item should be split into multiple independently "
                    "executable atomic actions. Each split becomes a separate tool call (e.g. a "
                    "separate Jira ticket, a separate document, a separate message).\n\n"
                    "SPLIT — these each become SEPARATE tools/tickets:\n"
                    '  "Investigate flaky tests and fix them"\n'
                    '    → ["Investigate flaky tests", "Fix flaky tests"]\n'
                    '  "Circle back to flaky tests to investigate and resolve the issue"\n'
                    '    → ["Investigate flaky tests", "Resolve flaky tests issue"]\n'
                    '  "Write documentation and publish it to Notion"\n'
                    '    → ["Write documentation", "Publish documentation to Notion"]\n\n'
                    "DO NOT split — these are ONE cohesive tool call:\n"
                    '  "Create and track a task for fixing alerts"  → single Jira ticket\n'
                    '  "Draft and send email to client"             → single email action\n'
                    '  "Schedule a meeting and invite participants"  → single calendar event\n\n'
                    "Key rule: if removing one verb leaves a still-useful, independent action, split.\n\n"
                    "Return JSON:\n"
                    '  {{"should_split": true/false, "splits": ["atomic desc 1", "atomic desc 2", ...]}}\n'
                    "If should_split is false, return splits as [].",
                ),
                ("human", "Action: {description}\nAssignee: {assignee}"),
            ]
        )
        chain = prompt | structured_llm
        result = chain.invoke(
            {
                "description": action.description,
                "assignee": action.assignee or "unknown",
            },
            config=config,
        )

        if result.should_split and len(result.splits) >= 2:
            parent_id = action.id
            split_actions: List[NormalizedAction] = []
            for split_desc in result.splits:
                split_desc = split_desc.strip()
                if not split_desc:
                    continue
                split_verb_raw = _extract_verb_from_desc(split_desc) or action.verb
                split_verb = split_verb_raw.replace(" ", "_")
                lookup = split_verb.replace("_", " ")
                split_verb_final = VERB_UPGRADES.get(
                    lookup, VERB_UPGRADES.get(split_verb, split_verb)
                )
                split_actions.append(
                    NormalizedAction(
                        id=str(uuid.uuid4())[:8],
                        description=split_desc,
                        assignee=action.assignee,
                        raw_deadline=action.raw_deadline,
                        normalized_deadline=action.normalized_deadline,
                        speaker=action.speaker,
                        verb=split_verb_final,
                        confidence=action.confidence,
                        tool_type=ToolType.GENERAL_TASK,
                        tool_params={},
                        source_spans=list(action.source_spans),
                        meeting_window=action.meeting_window,
                        action_category=action.action_category,
                        topic_tags=list(action.topic_tags),
                        parent_id=parent_id,
                    )
                )
            logger.info(
                "ActionSplitter: Split %r into %d atomic actions",
                action.description[:60],
                len(split_actions),
            )
            return split_actions

    except Exception as exc:
        logger.warning(
            "ActionSplitter: LLM call failed for %r: %s", action.description[:60], exc
        )

    return [action]


def action_splitter_node(
    state: NormalizerState,
    config: Optional[RunnableConfig] = None,
) -> NormalizerState:
    """
    Detect and split compound action descriptions into atomic, tool-executable actions.

    Rule-based detection flags candidates; LLM makes the final split decision and
    generates clean sub-descriptions.
    """
    working = state.get("working_actions", [])
    result: List[NormalizedAction] = []

    for action in working:
        if _is_compound(action.description):
            logger.info(
                "ActionSplitter: Compound candidate: %r", action.description[:70]
            )
            result.extend(_llm_split_action(action, config=config))
        else:
            result.append(action)

    logger.info(
        "ActionSplitter: %d → %d actions after splitting", len(working), len(result)
    )
    return {**state, "working_actions": result}


# ===========================================================================
# NODE 4 — DEDUPLICATOR
# ===========================================================================


def deduplicator_node(state: NormalizerState) -> NormalizerState:
    """
    Remove semantically duplicate actions.

    Two actions are considered duplicates when ALL of:
      (a) Same assignee (or at least one is null)
      (b) Same verb
      (c) Description Jaccard similarity ≥ 0.6 (ignoring stop words)

    The representative is the highest-confidence action; source_spans are merged.
    """
    working = state.get("working_actions", [])
    absorbed: set[int] = set()
    deduplicated: List[NormalizedAction] = []

    for i, a1 in enumerate(working):
        if i in absorbed:
            continue

        group: List[NormalizedAction] = [a1]
        for j, a2 in enumerate(working[i + 1:], start=i + 1):
            if j in absorbed:
                continue
            # Assignee must match (or one is null)
            if a1.assignee and a2.assignee:
                if a1.assignee.lower() != a2.assignee.lower():
                    continue
            # Verb must match
            if a1.verb and a2.verb and a1.verb != a2.verb:
                continue
            # Description similarity threshold
            if _jaccard_similarity(a1.description, a2.description) < 0.6:
                continue
            group.append(a2)
            absorbed.add(j)

        if len(group) == 1:
            deduplicated.append(a1)
        else:
            representative = max(group, key=lambda a: a.confidence)
            merged_spans = list(
                dict.fromkeys(span for a in group for span in a.source_spans)
            )
            deduplicated.append(
                representative.model_copy(update={"source_spans": merged_spans})
            )
            logger.info(
                "Deduplicator: Merged %d duplicates → %r",
                len(group),
                representative.description[:60],
            )

    logger.info(
        "Deduplicator: %d → %d actions after deduplication", len(working), len(deduplicated)
    )
    return {**state, "working_actions": deduplicated}


# ===========================================================================
# NODE 5 — TOOL CLASSIFIER
# ===========================================================================


class _ToolClassificationResult(PydanticBaseModel):
    tool_types: List[str]


_LLM_TOOL_TYPE_MAP: dict[str, ToolType] = {
    "send_email": ToolType.SEND_EMAIL,
    "create_jira_task": ToolType.CREATE_JIRA_TASK,
    "set_calendar": ToolType.SET_CALENDAR,
    "create_notion_doc": ToolType.CREATE_NOTION_DOC,
    "send_notification": ToolType.SEND_NOTIFICATION,
    "general_task": ToolType.GENERAL_TASK,
}


def tool_classifier_node(
    state: NormalizerState,
    config: Optional[RunnableConfig] = None,
) -> NormalizerState:
    """
    Classify each action into a ToolType and extract tool-specific parameters.

    Steps:
      1. Rule-based classification via verb + action_category + description keywords.
      2. LLM batch call for any remaining GENERAL_TASK (unclassified) actions.
      3. Extract tool_params via regex for every classified action.
    """
    working = state.get("working_actions", [])
    updated: List[NormalizedAction] = []
    llm_indices: List[int] = []

    for action in working:
        tool_type = _classify_tool(action)
        updated.append(action.model_copy(update={"tool_type": tool_type}))
        if tool_type == ToolType.GENERAL_TASK:
            llm_indices.append(len(updated) - 1)

    # LLM fallback for unclassified actions
    if llm_indices:
        logger.info(
            "ToolClassifier: %d actions need LLM tool classification", len(llm_indices)
        )
        try:
            llm = create_llm(LOCAL_EXTRACTOR_CONFIG)
            structured_llm = llm.with_structured_output(_ToolClassificationResult)
            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "Classify each action item into exactly one tool type.\n\n"
                        "Available types:\n"
                        "  send_email          — drafting or sending a formal email to a person or client\n"
                        "  send_notification   — informal message, Slack/chat, or verbal update to someone\n"
                        "  create_jira_task    — bug fix, investigation, review, technical work item, ticket\n"
                        "  set_calendar        — scheduling a meeting, session, or any calendar event\n"
                        "  create_notion_doc   — writing documentation, runbooks, notes, or reference material\n"
                    "  general_task        — anything that doesn't fit the above\n\n"
                    'Return JSON: {{"tool_types": ["type1", "type2", ...]}} in the same order as input.',
                    ),
                    ("human", "Action items:\n{actions}"),
                ]
            )
            chain = prompt | structured_llm
            actions_text = "\n".join(
                f"{k + 1}. [{updated[i].verb}] {updated[i].description}"
                for k, i in enumerate(llm_indices)
            )
            result = chain.invoke({"actions": actions_text}, config=config)

            if result.tool_types and len(result.tool_types) == len(llm_indices):
                for orig_idx, type_str in zip(llm_indices, result.tool_types):
                    tt = _LLM_TOOL_TYPE_MAP.get(
                        type_str.lower().strip(), ToolType.GENERAL_TASK
                    )
                    updated[orig_idx] = updated[orig_idx].model_copy(
                        update={"tool_type": tt}
                    )
        except Exception as exc:
            logger.warning("ToolClassifier: LLM fallback failed: %s", exc)

    # Extract tool_params for every action now that tool_type is finalised
    final: List[NormalizedAction] = []
    for action in updated:
        params = _extract_tool_params(action)
        final.append(action.model_copy(update={"tool_params": params}))

    logger.info("ToolClassifier: %d actions classified and parameterised", len(final))
    return {**state, "working_actions": final}
