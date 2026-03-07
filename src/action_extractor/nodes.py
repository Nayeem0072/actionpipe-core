"""Individual nodes for LangGraph action item extraction."""
import re
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, List
from pydantic import BaseModel as PydanticBaseModel
from langchain_core.prompts import ChatPromptTemplate

from .state import GraphState
from .models import Segment, Action, ActionDetails
from .llm_config import LOCAL_EXTRACTOR_CONFIG, CROSS_CHUNK_RESOLVER_CONFIG

logger = logging.getLogger(__name__)

# Regex to extract the primary action verb from a description string.
# Multi-word phrases are listed before single words to ensure the longest match wins.
_LEADING_VERB_RE = re.compile(
    r"^(circle\s+back|follow\s+up|follow\s+through|talk\s+to|speak\s+with|"
    r"reach\s+out|check\s+in|check\s+on|look\s+into|set\s+up|clean\s+up|"
    r"write\s+up|take\s+care\s+of|deal\s+with|go\s+over|"
    r"draft|send|email|schedule|book|create|fix|investigate|review|check|add|"
    r"notify|inform|tell|document|write|track|update|test|resolve|implement|"
    r"deploy|monitor|configure|refactor|migrate|analyze|discuss|prepare|submit|"
    r"approve|assign|complete|build|research|audit|remove|delete|verify|confirm|"
    r"coordinate|ensure|present|push|pull|run|execute|close|open|start|stop|"
    r"enable|disable|escalate|triage|unblock|validate|reproduce)\b",
    re.IGNORECASE,
)

# Cap concurrent LLM API calls to avoid rate-limiting on high-chunk-count transcripts.
# Raise this if your API tier supports higher concurrency.
_MAX_PARALLEL_CHUNKS = 6

# Retry thresholds for under-extracted chunks.
# A retry is only attempted when BOTH conditions hold:
#   1. The chunk's relevance score is high (the chunk looks substantive).
#   2. The segment yield is below the minimum expected ratio.
# This prevents wasting retries on legitimately quiet/social stretches of conversation.
_MIN_SEGMENTS_PER_TURN_RATIO = 1 / 5    # expect at least 1 segment per 5 turns
_HIGH_RELEVANCE_SCORE_THRESHOLD = 3     # score >= 3 means the chunk has multiple action signals
_MAX_EXTRACTION_RETRIES = 2             # up to 2 extra attempts after the original


# ===========================================================================
# LLM FACTORY
# ===========================================================================

def create_llm(cfg: dict):
    """
    Unified LLM factory.

    Branches on cfg["provider"]:
      - "claude"  -> ChatAnthropic        (Anthropic API)
      - "gemini"  -> ChatGoogleGenerativeAI (Google Generative AI API)
      - "ollama"  -> ChatOpenAI           (OpenAI-compatible, custom base_url)

    To add a new provider (e.g. "openai" for GPT):
      1. Create configs/gpt.env with PROVIDER=openai
      2. Add an elif branch here
    """
    provider = cfg.get("provider", "ollama").lower()

    if provider == "claude":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=cfg["model_name"],
            api_key=cfg.get("api_key") or None,
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
            timeout=cfg.get("timeout", 60),
        )

    elif provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=cfg["model_name"],
            google_api_key=cfg.get("api_key") or None,
            temperature=cfg["temperature"],
            max_output_tokens=cfg["max_tokens"],
        )

    elif provider == "ollama":
        import httpx
        from langchain_openai import ChatOpenAI
        extra_body = {
            "top_p": cfg["top_p"],
            "repeat_penalty": cfg["repeat_penalty"],
            "presence_penalty": cfg["presence_penalty"],
        }
        timeout_sec = cfg.get("timeout", 60)
        return ChatOpenAI(
            base_url=cfg["api_url"],
            api_key=cfg.get("api_key") or "not-needed",
            model=cfg["model_name"],
            temperature=cfg["temperature"],
            max_tokens=cfg["max_tokens"],
            extra_body=extra_body,
            timeout=httpx.Timeout(timeout_sec),
            max_retries=0,
        )

    else:
        raise ValueError(
            f"Unsupported provider '{provider}'. "
            "Add a branch in create_llm() in nodes.py to support it."
        )


def create_local_extractor_llm():
    """Create LLM configured for the (combined) extractor node."""
    return create_llm(LOCAL_EXTRACTOR_CONFIG)


def create_cross_chunk_resolver_llm():
    """Create LLM configured for the cross-chunk resolver node."""
    return create_llm(CROSS_CHUNK_RESOLVER_CONFIG)


# ===========================================================================
# RULE-BASED RELEVANCE SCORING  (Change 2)
# ===========================================================================

_ACTION_KEYWORDS = [
    "will", "should", "need to", "needs to", "going to",
    "can you", "could you", "please", "follow up", "schedule",
    "by when", "deadline", "i'll", "we'll", "let's",
    "make sure", "track", "add to", "review", "fix", "update",
]


def _score_chunk_relevance(chunk_text: str) -> int:
    """
    Count how many action-oriented keywords appear in the chunk.
    Returns 0 for clearly irrelevant chunks (greetings, small-talk, tech glitches).
    score >= 1 → process; score == 0 → skip.
    """
    text = chunk_text.lower()
    return sum(1 for kw in _ACTION_KEYWORDS if kw in text)


# ===========================================================================
# SINGLE-CHUNK EXTRACTOR  (thread-safe helper for parallel execution)
# ===========================================================================

class _SegmentExtraction(PydanticBaseModel):
    segments: List[Dict[str, Any]]


def _parse_segments(result: _SegmentExtraction, chunk_index: int) -> List[Segment]:
    """
    Convert a raw _SegmentExtraction result into typed Segment objects.
    Segments with empty text are skipped and logged.
    Extracted so retry attempts can call it without duplicating code.
    """
    segments = []
    for idx, seg_data in enumerate(result.segments):
        text = seg_data.get("text", "")
        if not text:
            logger.warning("Extractor: Chunk %d segment %d has empty text, skipping", chunk_index + 1, idx)
            continue
        span_id = hashlib.md5(f"{chunk_index}_{idx}_{text}".encode()).hexdigest()[:12]

        action_details = None
        if seg_data.get("intent") == "action_item" and seg_data.get("action_details"):
            ad_data = seg_data["action_details"]
            raw_tags = ad_data.get("topic_tags") or []
            action_details = ActionDetails(
                description=ad_data.get("description"),
                assignee=ad_data.get("assignee"),
                deadline=ad_data.get("deadline"),
                confidence=ad_data.get("confidence"),
                topic_tags=[t.lower().strip() for t in raw_tags if isinstance(t, str) and t.strip()],
                unresolved_reference=ad_data.get("unresolved_reference") if seg_data.get("context_unclear") else None,
                action_category=ad_data.get("action_category"),
            )

        segments.append(Segment(
            speaker=seg_data.get("speaker", ""),
            text=text,
            intent=seg_data.get("intent", "information"),
            resolved_context=seg_data.get("resolved_context", ""),
            context_unclear=seg_data.get("context_unclear", False),
            action_details=action_details,
            span_id=span_id,
            chunk_index=chunk_index,
        ))

    return segments


def _extract_single_chunk(chunk: str, chunk_index: int, relevance_score: int) -> List[Segment]:
    """
    Extract candidate segments from one chunk. Creates its own LLM instance so
    it is safe to call concurrently from multiple threads.

    Retry logic: if the chunk looks substantive (relevance_score >= threshold) but the
    LLM returns suspiciously few segments (< min_expected), retry up to
    _MAX_EXTRACTION_RETRIES times. This catches partial/truncated structured-output
    responses without wasting retries on legitimately quiet stretches of conversation.
    """
    llm = create_local_extractor_llm()
    structured_llm = llm.with_structured_output(_SegmentExtraction)

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are extracting work-relevant segments from a meeting transcript chunk.

Extract segments that contain:
- Action items (tasks assigned or self-assigned)
- Decisions
- Suggestions with implied actions
- Important information about work

For each segment, identify:
- speaker: Who said it
- text: Exact text from transcript (must be exact substring)
- intent: suggestion | information | question | decision | action_item | agreement | clarification
- resolved_context: What this refers to (if applicable, else empty string)
- context_unclear: true if reference cannot be resolved from THIS chunk alone
  - action_details: Only for action_item intent:
  - description: FULL, SELF-CONTAINED description of what needs to be done. Use context from the chunk to expand pronouns and references. BAD: "draft it", "writing that down", "add to list". GOOD: "Draft email to Client Delta to reset expectations, including phased delivery plan and scope change impact", "Note to circle back to flaky tests later", "Add task for fixing monitoring alert rules to list". Always include enough detail that someone reading only the description understands the action.
  - assignee: Who is responsible (name or role)
  - deadline: Timeline mentioned (e.g. "March 10", "after the meeting", "end of month") or null
  - confidence: 0.0-1.0
  - topic_tags: 2-4 short lowercase keywords capturing the SUBJECT of the action, independent of verb and phrasing. These are used to match the same task if it is described differently in another part of the transcript. Examples: ["client", "email", "scope"] for anything about a client email; ["tests", "backend", "flaky"] for anything about fixing flaky tests; ["alert", "monitoring", "rules"] for anything about monitoring alerts. Use the same tags even if the description wording differs.
  - unresolved_reference: ONLY when context_unclear=true — a short phrase describing what is being referenced that could not be resolved from this chunk alone. Example: if someone says "ill handle it" and "it" refers to something mentioned before this chunk, write the best guess of what "it" is (e.g. "the gateway migration task", "what John mentioned about the tests"). Leave null when context_unclear=false.
  - action_category: Category of this action. Choose exactly one: "communication" (involves emailing, notifying, or messaging someone), "task" (involves fixing, investigating, reviewing, implementing, or tracking a work item in a ticket/backlog), "event" (involves scheduling or booking a meeting, session, or calendar event), "documentation" (involves writing, documenting, or creating reference material), "other" (none of the above).

CRITICAL for description: Resolve "it", "that", "this", "that thing" from nearby turns in this chunk. If someone says "ill draft it after this", look for what "it" refers to (e.g. "update email to client") and write that in the description."""),
        ("human", "Extract segments from this chunk:\n\n{chunk}"),
    ])

    chain = prompt | structured_llm

    # Chunks with fewer turns are expected to produce fewer segments.
    # Count turns by the double-newline separator used by the segmenter.
    turn_count = chunk.count("\n\n") + 1
    min_expected = max(1, int(turn_count * _MIN_SEGMENTS_PER_TURN_RATIO))
    # Only retry when the chunk looks substantive — prevents spurious retries on
    # legitimately quiet or social stretches that correctly produce few segments.
    should_retry_on_low_yield = relevance_score >= _HIGH_RELEVANCE_SCORE_THRESHOLD

    best_segments: List[Segment] = []
    for attempt in range(1, _MAX_EXTRACTION_RETRIES + 2):  # 1 original + up to N retries
        try:
            result = chain.invoke({"chunk": chunk})
        except Exception as e:
            logger.error(
                "Extractor: Chunk %d attempt %d LLM call failed: %s",
                chunk_index + 1, attempt, e,
            )
            if attempt <= _MAX_EXTRACTION_RETRIES:
                continue
            return best_segments

        segments = _parse_segments(result, chunk_index)

        if len(segments) > len(best_segments):
            best_segments = segments

        yield_ok = len(segments) >= min_expected
        retries_left = attempt <= _MAX_EXTRACTION_RETRIES

        if yield_ok or not should_retry_on_low_yield or not retries_left:
            if not yield_ok:
                logger.warning(
                    "Extractor: Chunk %d (relevance=%d) yielded only %d segment(s) "
                    "(expected >= %d, %d turns) after %d attempt(s) — using best result",
                    chunk_index + 1, relevance_score, len(best_segments),
                    min_expected, turn_count, attempt,
                )
            return best_segments

        logger.warning(
            "Extractor: Chunk %d (relevance=%d) attempt %d yielded only %d segment(s) "
            "(expected >= %d, %d turns) — retrying",
            chunk_index + 1, relevance_score, attempt,
            len(segments), min_expected, turn_count,
        )

    return best_segments


# ===========================================================================
# NODES
# ===========================================================================

def segmenter_node(state: GraphState) -> GraphState:
    """
    [1] SEGMENTER NODE
    Role: Structural chunking only (NO AI)
    Goal: Preserve conversational integrity.
    Logic: Split by speaker turns, group into 20 turns per chunk.
    """
    logger.info("Segmenter: Starting chunking...")

    transcript_raw = state.get("transcript_raw", "")
    if not transcript_raw:
        logger.warning("Segmenter: No transcript_raw in state")
        return {**state, "chunks": [], "chunk_index": 0}

    # Split by speaker turns (format: "Speaker: text")
    turn_pattern = re.compile(r'^([A-Za-z][A-Za-z0-9\s]+?):\s*(.+)$', re.MULTILINE)
    turns = []
    for match in turn_pattern.finditer(transcript_raw):
        speaker = match.group(1).strip()
        text = match.group(2).strip()
        if text:
            turns.append(f"{speaker}: {text}")

    logger.info("Segmenter: Found %d speaker turns", len(turns))

    # 20 turns per chunk — large enough that most intra-chunk references resolve
    # within the chunk, reducing the need for cross-chunk context resolution.
    chunk_size = 20
    chunks = []
    for i in range(0, len(turns), chunk_size):
        chunk = "\n\n".join(turns[i:i + chunk_size])
        logger.info("Segmenter: Chunk index: %d, length: %d", i, len(chunk))
        logger.info("Segmenter: Chunk text: %s", chunk)
        chunks.append(chunk)

    logger.info("Segmenter: Created %d chunks", len(chunks))

    result = {
        **state,
        "chunks": chunks,
        "chunk_index": 0,
        "candidate_segments": [],
        "unresolved_references": [],
        "active_topics": {},
        "merged_actions": [],
        "emitted_text_spans": set(),
    }
    progress_cb = state.get("progress_callback")
    if progress_cb and callable(progress_cb) and chunks:
        progress_cb("progress", {
            "agent": "extractor",
            "step": "chunks",
            "status": "running",
        })
    return result


def parallel_extractor_node(state: GraphState) -> GraphState:
    """
    [2] PARALLEL EXTRACTOR NODE  (replaces relevance_gate + local_extractor + context_resolver)

    Steps:
      1. Score every chunk with the rule-based keyword filter (free, instant).
      2. Submit all relevant chunks to a ThreadPoolExecutor for concurrent LLM extraction.
      3. Collect all Segment objects; sort by original chunk order.

    Wall time ≈ max(single_chunk_latency) instead of sum(all_chunk_latencies).
    """
    chunks = state.get("chunks", [])

    # Rule-based relevance filter — no LLM cost.
    # Compute the score once and keep it so it can be forwarded to the extractor
    # for use in the retry guard.
    scored = [(i, chunk, _score_chunk_relevance(chunk)) for i, chunk in enumerate(chunks)]
    relevant = [(i, chunk, score) for i, chunk, score in scored if score >= 1]
    skipped = len(chunks) - len(relevant)
    logger.info(
        "ParallelExtractor: %d/%d chunks relevant, %d skipped by keyword filter",
        len(relevant), len(chunks), skipped,
    )

    if not relevant:
        logger.info("ParallelExtractor: No relevant chunks found")
        return {**state, "candidate_segments": []}

    all_segments: List[Segment] = []
    # Map chunk index → segment list for post-hoc anomaly detection
    chunk_segment_map: Dict[int, List[Segment]] = {}
    max_workers = min(len(relevant), _MAX_PARALLEL_CHUNKS)
    total_chunks = len(relevant)
    progress_cb = state.get("progress_callback")

    logger.info("ParallelExtractor: Launching %d concurrent extraction tasks", len(relevant))
    completed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk = {
            executor.submit(_extract_single_chunk, chunk, idx, score): idx
            for idx, chunk, score in relevant
        }
        for future in as_completed(future_to_chunk):
            idx = future_to_chunk[future]
            try:
                segments = future.result()
                all_segments.extend(segments)
                chunk_segment_map[idx] = segments
                completed += 1
                if progress_cb and callable(progress_cb):
                    progress_cb("progress", {
                        "agent": "extractor",
                        "step": "parallel_extractor",
                        "status": "running",
                        "current": completed,
                        "total": total_chunks,
                    })
                logger.info("ParallelExtractor: Chunk %d completed, %d segments (%d/%d)", idx + 1, len(segments), completed, total_chunks)
            except Exception as exc:
                logger.error("ParallelExtractor: Chunk %d raised an exception: %s", idx + 1, exc)
                chunk_segment_map[idx] = []

    # Post-hoc anomaly check: warn if any chunk's yield is far below the average.
    # This catches failures that survived all retries (e.g. model consistently
    # returns empty responses for a specific chunk).
    if chunk_segment_map:
        avg = sum(len(s) for s in chunk_segment_map.values()) / len(chunk_segment_map)
        if avg > 0:
            for idx, segs in chunk_segment_map.items():
                if len(segs) < avg * 0.3:
                    logger.warning(
                        "ParallelExtractor: Chunk %d has only %d segment(s) vs avg %.1f "
                        "— may be under-extracted even after retries",
                        idx + 1, len(segs), avg,
                    )

    # Restore original chunk order (as_completed returns in completion order)
    all_segments.sort(key=lambda s: s.chunk_index)
    logger.info(
        "ParallelExtractor: %d total segments from %d relevant chunks",
        len(all_segments), len(relevant),
    )

    return {**state, "candidate_segments": all_segments}


# Utterances that acknowledge a task is being noted/recorded rather than
# creating a new task. These should never become action items.
_META_ACTION_PATTERNS = re.compile(
    r"^\s*("
    r"adding|noted|noting(\s+that)?|writing\s+that\s+down|"
    r"i'?ll\s+(add|note|write|put)\s+(it|that(\s+down)?)|"
    r"(i'?ll\s+)?add\s+(it|that)\s+to\s+(the\s+)?(list|board|backlog|tracker)|"
    r"putting\s+(it|that)\s+(in|on)\s+(the\s+)?(list|board|backlog|tracker)|"
    r"got\s+it|on\s+it|done|copy\s+that|roger(\s+that)?"
    r")\s*$",
    re.IGNORECASE,
)


def evidence_normalizer_node(state: GraphState) -> GraphState:
    """
    [3] EVIDENCE NORMALIZER NODE
    Role: Structure cleaning (no heavy reasoning)
    Standardizes verbs, trims ASR noise, removes duplicates.
    Also drops meta-action utterances (e.g. "adding", "noted") that acknowledge
    a task is being recorded rather than creating a new task.
    After normalisation, converts all action_item segments into Action objects.
    """
    segments = state.get("candidate_segments", [])
    logger.info("EvidenceNormalizer: Normalizing %d segments", len(segments))

    verb_normalizations = {
        "take care of": "fix",
        "take care": "fix",
        "handle": "fix",
        "deal with": "fix",
        "we should": "suggestion",
        "let's": "suggestion",
        "need to": "fix",
        "gonna": "will",
        "wanna": "want",
    }

    normalized_segments = []
    seen_texts: set = set()

    for seg in segments:
        # Trim ASR noise
        text = seg.text
        text = re.sub(r'\b(um|uh|er|ah|like|you know)\b', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text).strip()

        if not text:
            logger.info("EvidenceNormalizer: Dropping segment with empty text (original: %r)", seg.text)
            continue

        # Drop meta-action utterances
        if seg.intent == "action_item" and _META_ACTION_PATTERNS.match(text):
            logger.info("EvidenceNormalizer: Dropping meta-action segment: %r", text)
            continue

        # Skip exact duplicates across all chunks
        text_lower = text.lower()
        if text_lower in seen_texts:
            logger.info("EvidenceNormalizer: Dropping duplicate segment: %r", text)
            continue
        seen_texts.add(text_lower)

        # Normalize verbs in action items
        raw_verb = None
        if seg.intent == "action_item" and seg.action_details:
            desc = seg.action_details.description or ""
            for pattern, replacement in verb_normalizations.items():
                if pattern.lower() in desc.lower():
                    raw_verb = replacement
                    break

        normalized_segments.append(Segment(
            speaker=seg.speaker,
            text=text,
            intent=seg.intent,
            resolved_context=seg.resolved_context,
            context_unclear=seg.context_unclear,
            action_details=seg.action_details,
            span_id=seg.span_id,
            chunk_index=seg.chunk_index,
            raw_verb=raw_verb,
        ))

    logger.info("EvidenceNormalizer: %d segments after normalization", len(normalized_segments))

    # Convert action_item segments into Action objects
    actions: List[Action] = []
    for seg in normalized_segments:
        if seg.intent == "action_item" and seg.action_details:
            actions.append(Action(
                description=seg.action_details.description or seg.text,
                assignee=seg.action_details.assignee or seg.speaker,
                deadline=seg.action_details.deadline,
                speaker=seg.speaker,
                verb=seg.raw_verb or "do",
                object_text=None,
                confidence=seg.action_details.confidence or 0.7,
                source_spans=[seg.span_id],
                meeting_window=(seg.chunk_index, seg.chunk_index),
                topic_tags=seg.action_details.topic_tags,
                unresolved_reference=seg.action_details.unresolved_reference,
                action_category=seg.action_details.action_category,
            ))

    logger.info("EvidenceNormalizer: Created %d action items from normalized segments", len(actions))

    return {**state, "candidate_segments": normalized_segments, "merged_actions": actions}


def _apply_cross_chunk_resolution(
    actions: List[Action],
    merge_groups: List[List[int]],
    updates: List[Dict[str, Any]],
) -> List[Action]:
    """
    Apply merge groups and field updates returned by the cross-chunk resolver LLM.
    Pure logic — no LLM calls.
    """
    actions = [a.model_copy() for a in actions]  # shallow copy to avoid mutating originals

    # Apply field updates first (before merging, so merged representative gets updates)
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        idx = upd.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(actions):
            continue
        if "description" in upd and upd["description"]:
            actions[idx].description = upd["description"]
        if "assignee" in upd and upd["assignee"]:
            actions[idx].assignee = upd["assignee"]
        if "deadline" in upd and upd["deadline"]:
            actions[idx].deadline = upd["deadline"]

    # Apply merge groups
    absorbed: set = set()
    merged: List[Action] = []

    for group in merge_groups:
        valid = [i for i in group if isinstance(i, int) and 0 <= i < len(actions)]
        if len(valid) < 2:
            continue
        for i in valid:
            absorbed.add(i)

        group_actions = [actions[i] for i in valid]
        # Representative: the one with the longest (most specific) description
        representative = max(group_actions, key=lambda a: len(a.description or ""))
        for other in group_actions:
            if other is representative:
                continue
            if not representative.assignee and other.assignee:
                representative.assignee = other.assignee
            if not representative.deadline and other.deadline:
                representative.deadline = other.deadline
            representative.source_spans = list(set(representative.source_spans + other.source_spans))
            representative.confidence = max(representative.confidence, other.confidence)
            # Merge topic tags
            existing_tags = set(representative.topic_tags)
            for tag in other.topic_tags:
                if tag not in existing_tags:
                    representative.topic_tags.append(tag)
                    existing_tags.add(tag)
            # Expand meeting window to cover both chunks
            if representative.meeting_window and other.meeting_window:
                representative.meeting_window = (
                    min(representative.meeting_window[0], other.meeting_window[0]),
                    max(representative.meeting_window[1], other.meeting_window[1]),
                )
        merged.append(representative)

    # Rebuild final list: non-absorbed actions (in original order) + merged representatives
    result: List[Action] = []
    for i, action in enumerate(actions):
        if i not in absorbed:
            result.append(action)
    result.extend(merged)

    # Re-sort chronologically
    result.sort(key=lambda a: a.meeting_window[0] if a.meeting_window else 999)
    return result


def cross_chunk_resolver_node(state: GraphState) -> GraphState:
    """
    [4] CROSS-CHUNK RESOLVER NODE

    Runs a single LLM call over all extracted actions to:
      1. Identify actions that describe the same real-world task using different vocabulary
         (leveraging topic_tags for vocabulary-independent matching).
      2. Resolve vague descriptions where unresolved_reference signals a cross-chunk pronoun
         ("I'll do that" from chunk N referring to a task in chunk N-1).
      3. Attribute missing deadline/assignee from a later action to an earlier related one.

    Skipped entirely when there is only one chunk or fewer than 2 actions (nothing to resolve).
    Falls back gracefully to the unmodified action list if the LLM call fails.
    """
    actions = state.get("merged_actions", [])
    chunks = state.get("chunks", [])

    if len(chunks) <= 1 or len(actions) < 2:
        logger.info("CrossChunkResolver: Skipping (only %d chunk(s), %d action(s))", len(chunks), len(actions))
        return state

    # Build a compact representation for the prompt
    action_lines = []
    for i, act in enumerate(actions):
        tags_str = ",".join(act.topic_tags) if act.topic_tags else "—"
        unref_str = f'  unresolved_ref="{act.unresolved_reference}"' if act.unresolved_reference else ""
        chunk_num = act.meeting_window[0] if act.meeting_window else "?"
        action_lines.append(
            f"[{i}] chunk={chunk_num}  speaker={act.speaker}  tags=[{tags_str}]{unref_str}\n"
            f"    \"{act.description}\""
        )
    actions_text = "\n".join(action_lines)

    class CrossChunkResolution(PydanticBaseModel):
        merge_groups: List[List[int]] = []
        updates: List[Dict[str, Any]] = []

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are reviewing action items extracted in parallel from different parts of a meeting transcript.
Because they were extracted independently, some may be duplicates described in different words,
or may have vague descriptions that can be clarified using context from nearby actions.

Your tasks:
1. MERGE DUPLICATES: Identify groups of actions that refer to the same real-world task.
   Use topic_tags as the primary signal — overlapping tags strongly suggest the same task.
   Also compare descriptions semantically, not just by word overlap.
   Return each group of duplicate indices in merge_groups (only groups of 2+).

2. RESOLVE VAGUE REFERENCES: For any action with unresolved_ref, find the most likely
   matching action from another chunk and rewrite the description to be fully self-contained.
   Return as an update: {{index: N, description: "new self-contained description"}}.

3. LINK MISSING FIELDS: If a later action provides a deadline or assignee that clearly
   belongs to an earlier related action, return an update: {{index: N, deadline: "..."}} or
   {{index: N, assignee: "..."}}.

Rules:
- Only merge when you are confident it is the same task. Do NOT merge actions about different things.
- Do NOT change descriptions unless the current one is genuinely vague or incomplete.
- Return empty lists if nothing needs to be merged or updated.

Return JSON with:
{{
  "merge_groups": [[i, j], ...],
  "updates": [{{"index": N, "description": "...", "deadline": "...", "assignee": "..."}}, ...]
}}"""),
        ("human", "Action items to review:\n\n{actions}"),
    ])

    llm = create_cross_chunk_resolver_llm()
    structured_llm = llm.with_structured_output(CrossChunkResolution)
    chain = prompt | structured_llm

    logger.info(
        "CrossChunkResolver: Reviewing %d actions across %d chunks",
        len(actions), len(chunks),
    )

    try:
        result = chain.invoke({"actions": actions_text})
        merge_groups = result.merge_groups or []
        updates = result.updates or []
        logger.info(
            "CrossChunkResolver: %d merge group(s), %d update(s)",
            len(merge_groups), len(updates),
        )
    except Exception as e:
        logger.warning("CrossChunkResolver: LLM call failed (%s) — passing through unchanged", e)
        return state

    if not merge_groups and not updates:
        logger.info("CrossChunkResolver: No changes needed")
        return state

    resolved = _apply_cross_chunk_resolution(actions, merge_groups, updates)
    logger.info(
        "CrossChunkResolver: %d actions → %d after resolution",
        len(actions), len(resolved),
    )
    return {**state, "merged_actions": resolved}


def global_deduplicator_node(state: GraphState) -> GraphState:
    """
    [4] GLOBAL DEDUPLICATOR NODE
    Role: Remove duplicate actions across all chunks.
    Two actions are considered duplicates if:
    - verb similar
    - description has sufficient word overlap (>= 40%)
    - occur in same meeting window (within 3 chunks)
    Speaker is intentionally NOT required to match: the same task can be raised by
    one person and acknowledged/noted by another.
    """
    merged_actions = state.get("merged_actions", [])
    logger.info("GlobalDeduplicator: Processing %d actions", len(merged_actions))

    _STOP_WORDS = {
        "a", "an", "the", "to", "for", "of", "and", "or", "in", "on", "at",
        "it", "that", "this", "is", "be", "with", "as", "by", "from", "up",
        "task", "item", "list", "add", "create", "note",
    }

    def _content_words(text: str) -> set:
        return {w for w in text.lower().split() if w not in _STOP_WORDS}

    def are_similar(action1: Action, action2: Action) -> bool:
        verb1 = (action1.verb or "").lower()
        verb2 = (action2.verb or "").lower()
        if verb1 and verb2 and verb1 != verb2:
            verb_synonyms = {
                "fix": ["fix", "handle", "take care", "deal"],
                "send": ["send", "email", "share"],
                "review": ["review", "check", "look"],
            }
            similar = False
            for syn_group in verb_synonyms.values():
                if verb1 in syn_group and verb2 in syn_group:
                    similar = True
                    break
            if not similar:
                return False

        words1 = _content_words(action1.description or "")
        words2 = _content_words(action2.description or "")
        if words1 and words2:
            overlap = len(words1 & words2) / max(len(words1), len(words2))
            if overlap < 0.4:
                return False

        if action1.meeting_window and action2.meeting_window:
            if abs(action1.meeting_window[0] - action2.meeting_window[0]) > 3:
                return False

        return True

    deduplicated = []
    seen_indices: set = set()

    for i, action1 in enumerate(merged_actions):
        if i in seen_indices:
            continue

        similar_group = [action1]
        for j, action2 in enumerate(merged_actions[i + 1:], start=i + 1):
            if j in seen_indices:
                continue
            if are_similar(action1, action2):
                similar_group.append(action2)
                seen_indices.add(j)

        if len(similar_group) == 1:
            deduplicated.append(action1)
        else:
            def _speaker_is_assignee(a: Action) -> bool:
                return bool(a.speaker and a.assignee and a.speaker.lower() == a.assignee.lower())

            representative = next(
                (a for a in similar_group if _speaker_is_assignee(a)),
                similar_group[0],
            )
            for other in similar_group:
                if other is representative:
                    continue
                if not representative.assignee and other.assignee:
                    representative.assignee = other.assignee
                if not representative.deadline and other.deadline:
                    representative.deadline = other.deadline
                representative.source_spans.extend(other.source_spans)
                representative.confidence = max(representative.confidence, other.confidence)
            deduplicated.append(representative)

    logger.info("GlobalDeduplicator: Reduced %d -> %d actions", len(merged_actions), len(deduplicated))
    return {**state, "merged_actions": deduplicated}


def action_finalizer_node(state: GraphState) -> GraphState:
    """
    [5] ACTION FINALIZER NODE
    Role: Enforce output schema.
    - Fill nulls
    - Normalize verbs
    - Drop low-confidence actions (< 0.3)
    - Sort chronologically by meeting window
    """
    merged_actions = state.get("merged_actions", [])
    logger.info("ActionFinalizer: Finalizing %d actions", len(merged_actions))

    finalized = []

    for action in merged_actions:
        if not action.description:
            continue

        verb = action.verb or "do"

        # When the verb is the generic placeholder "do", try to extract a real
        # verb phrase from the beginning of the description (rule-based, no LLM).
        if verb == "do":
            m = _LEADING_VERB_RE.match(action.description)
            if m:
                verb = m.group(1).lower().replace(" ", "_")

        verb_normalizations = {
            "take care of": "fix",
            "take_care_of": "fix",
            "handle": "fix",
            "deal with": "fix",
            "deal_with": "fix",
            "send": "send",
            "email": "send",
            "review": "review",
            "check": "review",
        }
        for pattern, normalized in verb_normalizations.items():
            if pattern.lower() in verb.lower():
                verb = normalized
                break

        if action.confidence and action.confidence < 0.3:
            logger.debug("ActionFinalizer: Dropping low-confidence action: %s", action.description)
            continue

        finalized.append(Action(
            description=action.description,
            assignee=action.assignee or action.speaker,
            deadline=action.deadline,
            speaker=action.speaker,
            verb=verb,
            object_text=action.object_text,
            confidence=action.confidence or 0.5,
            source_spans=list(set(action.source_spans)),
            meeting_window=action.meeting_window,
            topic_tags=action.topic_tags,
            unresolved_reference=action.unresolved_reference,
            action_category=action.action_category,
        ))

    finalized.sort(key=lambda a: a.meeting_window[0] if a.meeting_window else 999)

    logger.info("ActionFinalizer: Finalized %d actions (source_spans, meeting_window)", len(finalized))
    for i, act in enumerate(finalized, 1):
        logger.info(
            "[ACTION] #%d description=%s | source_spans=%s | meeting_window=%s",
            i,
            act.description,
            act.source_spans,
            list(act.meeting_window) if act.meeting_window else None,
        )

    return {**state, "merged_actions": finalized}
