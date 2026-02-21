"""Individual nodes for LangGraph action item extraction."""
import re
import hashlib
import logging
from typing import Dict, Any
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_core.output_parsers import JsonOutputParser

from .langgraph_state import GraphState
from .langgraph_models import Segment, Action, ActionDetails
from .langgraph_llm_config import (
    RELEVANCE_GATE_CONFIG,
    LOCAL_EXTRACTOR_CONFIG,
    CONTEXT_RESOLVER_CONFIG,
)

logger = logging.getLogger(__name__)


def create_relevance_gate_llm():
    """Create LLM configured for relevance gate node."""
    cfg = RELEVANCE_GATE_CONFIG
    extra_body = {
        "top_p": cfg["top_p"],
        "repeat_penalty": cfg["repeat_penalty"],
        "presence_penalty": cfg["presence_penalty"],
    }
    return ChatOpenAI(
        base_url=cfg["api_url"],
        api_key=cfg["api_key"] or "not-needed",
        model=cfg["model_name"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        extra_body=extra_body,
        timeout=cfg.get("timeout", 60),
    )


def create_local_extractor_llm():
    """Create LLM configured for local extractor node."""
    cfg = LOCAL_EXTRACTOR_CONFIG
    extra_body = {
        "top_p": cfg["top_p"],
        "repeat_penalty": cfg["repeat_penalty"],
        "presence_penalty": cfg["presence_penalty"],
    }
    return ChatOpenAI(
        base_url=cfg["api_url"],
        api_key=cfg["api_key"] or "not-needed",
        model=cfg["model_name"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        extra_body=extra_body,
        timeout=cfg.get("timeout", 120),
    )


def create_context_resolver_llm():
    """Create LLM configured for context resolver node."""
    import httpx
    cfg = CONTEXT_RESOLVER_CONFIG
    timeout_sec = cfg.get("timeout", 120)
    extra_body = {
        "top_p": cfg["top_p"],
        "repeat_penalty": cfg["repeat_penalty"],
        "presence_penalty": cfg["presence_penalty"],
    }
    # Explicit read timeout. max_retries=0 so one timeout fails after timeout_sec, not 3x (e.g. 360s).
    return ChatOpenAI(
        base_url=cfg["api_url"],
        api_key=cfg["api_key"] or "not-needed",
        model=cfg["model_name"],
        temperature=cfg["temperature"],
        max_tokens=cfg["max_tokens"],
        extra_body=extra_body,
        timeout=httpx.Timeout(timeout_sec),
        max_retries=0,
    )


def segmenter_node(state: GraphState) -> GraphState:
    """
    [1] SEGMENTER NODE
    Role: Structural chunking only (NO AI)
    Goal: Preserve conversational integrity.
    Logic: Split by speaker turns, group into 8-15 turns per chunk
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
        if text:  # Skip empty turns
            turns.append(f"{speaker}: {text}")
    
    logger.info(f"Segmenter: Found {len(turns)} speaker turns")
    
    # Group into chunks of 8-15 turns
    chunk_size = 8  # Target size
    chunks = []
    for i in range(0, len(turns), chunk_size):
        chunk = "\n\n".join(turns[i:i+chunk_size])
        logger.info(f"Segmenter: Chunk index: {i}, length: {len(chunk)}")
        logger.info(f"Segmenter: Chunk text: {chunk}")
        chunks.append(chunk)
    
    logger.info(f"Segmenter: Created {len(chunks)} chunks")
    
    return {
        **state,
        "chunks": chunks,
        "chunk_index": 0,
        "candidate_segments": [],
        "unresolved_references": [],
        "active_topics": {},
        "merged_actions": [],
        "emitted_text_spans": set(),
    }


def relevance_gate_node(state: GraphState) -> GraphState:
    """
    [2] RELEVANCE GATE NODE
    Role: Current GLM4.7Flash LLM filter
    Question: Does this chunk contain work-relevant operational content?
    Return: YES / NO
    """
    chunks = state.get("chunks", [])
    chunk_index = state.get("chunk_index", 0)
    
    if chunk_index >= len(chunks):
        logger.info("RelevanceGate: All chunks processed, ending workflow")
        return {**state, "relevance_result": "DONE"}
    
    chunk = chunks[chunk_index]
    logger.info(f"RelevanceGate: Checking chunk {chunk_index + 1}/{len(chunks)}")
    
    llm = create_relevance_gate_llm()
    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are a filter for meeting transcripts. Determine if a chunk contains work-relevant operational content.

RELEVANT content includes:
- Tasks, assignments, action items
- Decisions, plans, timelines
- Technical/business discussions
- Ownership, responsibilities
- Deadlines, schedules

NOT RELEVANT content includes:
- Greetings, small talk
- Jokes, filler words
- Technical glitches (audio issues, screen problems)
- Off-topic conversations

Respond with ONLY "YES" or "NO" (no explanation)."""),
        ("human", "Chunk:\n\n{chunk}"),
    ])
    
    chain = prompt | llm | StrOutputParser()
    result = chain.invoke({"chunk": chunk}).strip().upper()
    
    is_relevant = result.startswith("YES")
    logger.info(f"RelevanceGate: Chunk {chunk_index + 1} -> {result}")
    
    return {**state, "relevance_result": "YES" if is_relevant else "NO"}


def local_extractor_node(state: GraphState) -> GraphState:
    """
    [3] LOCAL EXTRACTOR NODE
    Role: Extract evidence from current chunk (NOT final truth)
    Produces candidate segments with action details
    """
    chunks = state.get("chunks", [])
    chunk_index = state.get("chunk_index", 0)
    chunk = chunks[chunk_index]
    
    logger.info(f"LocalExtractor: Extracting from chunk {chunk_index + 1}")
    
    llm = create_local_extractor_llm()
    
    # Use JSON mode for structured extraction
    from pydantic import BaseModel as PydanticBaseModel
    
    class SegmentExtraction(PydanticBaseModel):
        segments: list[Dict[str, Any]]
    
    structured_llm = llm.with_structured_output(SegmentExtraction, method="json_mode")
    
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
- context_unclear: true if reference cannot be resolved
- action_details: Only for action_item intent:
  - description: FULL, SELF-CONTAINED description of what needs to be done. Use context from the chunk to expand pronouns and references. BAD: "draft it", "writing that down", "add to list". GOOD: "Draft email to Client Delta to reset expectations, including phased delivery plan and scope change impact", "Note to circle back to flaky tests later", "Add task for fixing monitoring alert rules to list". Always include enough detail that someone reading only the description understands the action.
  - assignee: Who is responsible (name or role)
  - deadline: Timeline mentioned (e.g. "March 10", "after the meeting", "end of month") or null
  - confidence: 0.0-1.0

CRITICAL for description: Resolve "it", "that", "this", "that thing" from nearby turns. If someone says "ill draft it after this", look for what "it" refers to (e.g. "update email to client") and write that in the description."""),
        ("human", "Extract segments from this chunk:\n\n{chunk}"),
    ])
    
    chain = prompt | structured_llm
    result = chain.invoke({"chunk": chunk})
    
    # Convert to Segment objects
    segments = []
    for idx, seg_data in enumerate(result.segments):
        # Generate span ID
        text = seg_data.get("text", "")
        span_id = hashlib.md5(f"{chunk_index}_{idx}_{text}".encode()).hexdigest()[:12]
        
        action_details = None
        if seg_data.get("intent") == "action_item" and seg_data.get("action_details"):
            ad_data = seg_data["action_details"]
            action_details = ActionDetails(
                description=ad_data.get("description"),
                assignee=ad_data.get("assignee"),
                deadline=ad_data.get("deadline"),
                confidence=ad_data.get("confidence"),
            )
        
        segment = Segment(
            speaker=seg_data.get("speaker", ""),
            text=text,
            intent=seg_data.get("intent", "information"),
            resolved_context=seg_data.get("resolved_context", ""),
            context_unclear=seg_data.get("context_unclear", False),
            action_details=action_details,
            span_id=span_id,
            chunk_index=chunk_index,
        )
        segments.append(segment)
    
    logger.info(f"LocalExtractor: Extracted {len(segments)} segments")
    
    return {**state, "candidate_segments": segments}


def evidence_normalizer_node(state: GraphState) -> GraphState:
    """
    [4] EVIDENCE NORMALIZER NODE
    Role: Structure cleaning (no heavy reasoning)
    Standardizes verbs, trims ASR noise, removes duplicates, adds span IDs
    """
    segments = state.get("candidate_segments", [])
    logger.info(f"EvidenceNormalizer: Normalizing {len(segments)} segments")
    
    # Verb normalization mapping
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
    seen_texts = set()
    
    for seg in segments:
        # Trim ASR noise (common patterns)
        text = seg.text
        text = re.sub(r'\b(um|uh|er|ah|like|you know)\b', '', text, flags=re.IGNORECASE)
        text = re.sub(r'\s+', ' ', text).strip()
        
        # Skip if empty after cleaning
        if not text:
            continue
        
        # Skip duplicates within chunk (exact text match)
        text_lower = text.lower()
        if text_lower in seen_texts:
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
        
        # Create normalized segment
        normalized_seg = Segment(
            speaker=seg.speaker,
            text=text,
            intent=seg.intent,
            resolved_context=seg.resolved_context,
            context_unclear=seg.context_unclear,
            action_details=seg.action_details,
            span_id=seg.span_id,
            chunk_index=seg.chunk_index,
            raw_verb=raw_verb,
        )
        normalized_segments.append(normalized_seg)
    
    logger.info(f"EvidenceNormalizer: {len(normalized_segments)} segments after normalization")
    
    return {**state, "candidate_segments": normalized_segments}


# Max segments per chunk for which we call the context-resolver LLM (configurable in langgraph_llm_config).
# With more segments, the prompt and expected JSON are large; local models (e.g. Ollama)
# often time out or run very slow, so we skip the LLM and use the fallback (actions from
# action_item segments only, no cross-chunk linking).
def _get_context_resolver_max_segments():
    return CONTEXT_RESOLVER_CONFIG.get("max_segments_for_llm", 8)


def context_resolver_node(state: GraphState) -> GraphState:
    """
    [5] CONTEXT RESOLVER NODE (CORE INTELLIGENCE)
    Role: Cross-chunk reasoning
    Performs:
    - Reference Completion (attach objects to fragments)
    - Ownership Linking (connect "needs fixing" with "I'll handle")
    - Deadline Linking (update deadlines from later context)
    - Topic Tracking
    """
    candidate_segments = state.get("candidate_segments", [])
    unresolved_references = state.get("unresolved_references", [])
    active_topics = state.get("active_topics", {})
    merged_actions = state.get("merged_actions", [])
    chunk_index = state.get("chunk_index", 0)

    logger.info(f"ContextResolver: Resolving context for {len(candidate_segments)} new segments")
    logger.info(f"ContextResolver: {len(unresolved_references)} unresolved references, {len(active_topics)} active topics")

    max_segments = _get_context_resolver_max_segments()
    # Skip LLM when segment count is high: prompt + structured JSON output become large,
    # and local models often time out or take many minutes (e.g. chunk 2 with 12 segments).
    if len(candidate_segments) > max_segments:
        logger.info(
            "ContextResolver: Skipping LLM (segment count %d > %d); using fallback to avoid timeout.",
            len(candidate_segments),
            max_segments,
        )
        result = {
            "resolved_segments": [seg.model_dump() for seg in candidate_segments],
            "new_actions": [],
            "updated_actions": [],
            "still_unresolved": [],
        }
    else:
        result = _context_resolver_llm_call(
            candidate_segments,
            unresolved_references,
            active_topics,
            merged_actions,
            chunk_index,
        )

    # Normalize: Pydantic model has no .get(); convert to dict for uniform access
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    result = dict(result)  # ensure we have a dict

    # Process resolved segments - use all candidate segments for now
    resolved_segments = candidate_segments.copy()

    # Create new actions from action_item segments
    for seg in candidate_segments:
        if seg.intent == "action_item" and seg.action_details:
            existing_span_ids = {span for action in merged_actions for span in action.source_spans}
            if seg.span_id in existing_span_ids:
                continue
            action = Action(
                description=seg.action_details.description or seg.text,
                assignee=seg.action_details.assignee or seg.speaker,
                deadline=seg.action_details.deadline,
                speaker=seg.speaker,
                verb=seg.raw_verb or "do",
                object_text=None,
                confidence=seg.action_details.confidence or 0.7,
                source_spans=[seg.span_id],
                meeting_window=(chunk_index, chunk_index),
            )
            merged_actions.append(action)

    # Update existing actions
    for update_data in result.get("updated_actions", []):
        if not isinstance(update_data, dict):
            continue
        idx = update_data.get("index", -1)
        if 0 <= idx < len(merged_actions):
            if "deadline" in update_data:
                merged_actions[idx].deadline = update_data["deadline"]
            if "assignee" in update_data:
                merged_actions[idx].assignee = update_data["assignee"]

    # Track still unresolved
    still_unresolved = []
    for unresolved_data in result.get("still_unresolved", []):
        if not isinstance(unresolved_data, dict):
            continue
        for seg in candidate_segments:
            if seg.text == unresolved_data.get("text"):
                still_unresolved.append(seg)
                break

    # Update unresolved references
    new_unresolved = [ref for ref in unresolved_references if ref not in resolved_segments]
    new_unresolved.extend(still_unresolved)

    # Update active topics
    for seg in candidate_segments:
        if seg.intent in ["decision", "action_item"]:
            topic_key = seg.text[:50]
            active_topics[topic_key] = {
                "speaker": seg.speaker,
                "chunk": chunk_index,
                "resolved": seg.intent == "action_item",
            }

    new_actions_count = len([a for a in merged_actions if a.meeting_window and a.meeting_window[0] == chunk_index])
    logger.info(f"ContextResolver: Created {new_actions_count} new actions from this chunk")
    logger.info(f"ContextResolver: {len(new_unresolved)} unresolved references remaining")

    return {
        **state,
        "candidate_segments": resolved_segments,
        "unresolved_references": new_unresolved,
        "active_topics": active_topics,
        "merged_actions": merged_actions,
    }


def _context_resolver_llm_call(
    candidate_segments: list,
    unresolved_references: list,
    active_topics: dict,
    merged_actions: list,
    chunk_index: int,
) -> dict:
    """Call the LLM for context resolution. Returns dict or Pydantic model."""
    llm = create_context_resolver_llm()
    
    # Keep prompt small: local models time out on large context (e.g. chunk 8 with many
    # accumulated actions). Use last N actions and last N topics only.
    _max_prev_actions = CONTEXT_RESOLVER_CONFIG.get("max_previous_actions", 5)
    _max_topics = 3
    _max_unresolved = 3

    context_text = ""
    if unresolved_references:
        context_text += "Unresolved references:\n"
        for ref in unresolved_references[-_max_unresolved:]:
            context_text += f"- {ref.speaker}: {ref.text}\n"
    if active_topics:
        context_text += "\nActive topics:\n"
        for topic, info in list(active_topics.items())[-_max_topics:]:
            context_text += f"- {topic}: {info}\n"

    new_segments_text = "\n".join([
        f"{seg.speaker}: {seg.text} [{seg.intent}]"
        for seg in candidate_segments
    ])

    _actions_to_show = merged_actions[-_max_prev_actions:] if len(merged_actions) > _max_prev_actions else merged_actions
    _start_idx = len(merged_actions) - len(_actions_to_show)
    previous_actions_text = "\n".join([
        f"{_start_idx + i}. {act.description} (assignee: {act.assignee}, deadline: {act.deadline})"
        for i, act in enumerate(_actions_to_show)
    ])
    if len(merged_actions) > _max_prev_actions:
        previous_actions_text = f"(... {len(merged_actions) - _max_prev_actions} earlier omitted)\n" + previous_actions_text

    prompt = ChatPromptTemplate.from_messages([
        ("system", """You are resolving references and linking actions across meeting chunks.

Your tasks:
1. Complete references: If a segment says "I'll do that", link it to the most recent relevant topic
2. Link ownership: Connect vague mentions ("needs fixing") with specific commitments ("I'll handle")
3. Link deadlines: If a later segment mentions a deadline, attach it to earlier related actions
4. Track topics: Maintain active topic memory

For each new segment, determine:
- Does it complete a previous unresolved reference? (provide the link)
- Does it create a new action that should be merged with existing ones?
- Does it add deadline/assignee info to existing actions?

Return JSON with:
{{
  "resolved_segments": [...],
  "new_actions": [...],
  "updated_actions": [...],
  "still_unresolved": [...]
}}"""),
        ("human", """Context:
{context}

New segments from current chunk:
{new_segments}

Previous actions:
{previous_actions}"""),
    ])

    from pydantic import BaseModel as PydanticBaseModel, field_validator

    class ResolutionResult(PydanticBaseModel):
        resolved_segments: list[dict]
        new_actions: list[dict]
        updated_actions: list[dict]
        still_unresolved: list[dict]

        @field_validator("still_unresolved", mode="before")
        @classmethod
        def still_unresolved_to_dicts(cls, v: list) -> list:
            """Accept list of dicts or list of strings; normalize to list of dicts with 'text'."""
            if not isinstance(v, list):
                return v
            out = []
            for item in v:
                if isinstance(item, str):
                    out.append({"text": item})
                elif isinstance(item, dict):
                    out.append(item)
            return out

    structured_llm = llm.with_structured_output(ResolutionResult, method="json_mode")
    chain = prompt | structured_llm

    timeout_sec = CONTEXT_RESOLVER_CONFIG.get("timeout", 120)
    approx_prompt_chars = len(context_text) + len(new_segments_text) + len(previous_actions_text or "")
    logger.info(
        "ContextResolver: Calling LLM (timeout=%ds, prompt ~%d chars, %d previous actions)...",
        int(timeout_sec),
        approx_prompt_chars,
        len(_actions_to_show),
    )

    # Log full prompt so it can be copied and tried manually in Ollama if stuck
    try:
        formatted_messages = prompt.invoke({
            "context": context_text,
            "new_segments": new_segments_text,
            "previous_actions": previous_actions_text or "None",
        })
        for i, msg in enumerate(formatted_messages):
            role = getattr(msg, "type", "message")
            content = getattr(msg, "content", str(msg))
            logger.info("[CONTEXT_RESOLVER_PROMPT] --- Part %d (%s) ---\n%s", i + 1, role, content)
    except Exception as _e:
        logger.debug("Could not log full prompt: %s", _e)

    try:
        result = chain.invoke({
            "context": context_text,
            "new_segments": new_segments_text,
            "previous_actions": previous_actions_text or "None",
        })
    except Exception as e:
        logger.warning(f"ContextResolver: LLM resolution failed: {e}, using fallback")
        result = {
            "resolved_segments": [seg.model_dump() for seg in candidate_segments],
            "new_actions": [],
            "updated_actions": [],
            "still_unresolved": [],
        }
    return result


def global_deduplicator_node(state: GraphState) -> GraphState:
    """
    [6] GLOBAL DEDUPLICATOR NODE
    Role: Stop loops + repetition
    Two actions are the same if:
    - speaker same
    - verb similar
    - object similar
    - occur in same meeting window
    """
    merged_actions = state.get("merged_actions", [])
    logger.info(f"GlobalDeduplicator: Processing {len(merged_actions)} actions")
    
    def are_similar(action1: Action, action2: Action) -> bool:
        """Check if two actions are duplicates."""
        # Same speaker
        if action1.speaker != action2.speaker:
            return False
        
        # Similar verb (simple string similarity)
        verb1 = (action1.verb or "").lower()
        verb2 = (action2.verb or "").lower()
        if verb1 and verb2 and verb1 != verb2:
            # Check if verbs are synonyms
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
        
        # Similar object/description (simple word overlap)
        desc1_words = set((action1.description or "").lower().split())
        desc2_words = set((action2.description or "").lower().split())
        if desc1_words and desc2_words:
            overlap = len(desc1_words & desc2_words) / max(len(desc1_words), len(desc2_words))
            if overlap < 0.3:  # Less than 30% word overlap
                return False
        
        # Same meeting window (within 3 chunks)
        if action1.meeting_window and action2.meeting_window:
            window1 = action1.meeting_window
            window2 = action2.meeting_window
            if abs(window1[0] - window2[0]) > 3:
                return False
        
        return True
    
    # Deduplicate
    deduplicated = []
    seen_indices = set()
    
    for i, action1 in enumerate(merged_actions):
        if i in seen_indices:
            continue
        
        # Find all similar actions
        similar_group = [action1]
        for j, action2 in enumerate(merged_actions[i+1:], start=i+1):
            if j in seen_indices:
                continue
            if are_similar(action1, action2):
                similar_group.append(action2)
                seen_indices.add(j)
        
        # Merge the group
        if len(similar_group) == 1:
            deduplicated.append(action1)
        else:
            # Merge: take best assignee, deadline, combine spans
            merged = similar_group[0]
            for other in similar_group[1:]:
                if not merged.assignee and other.assignee:
                    merged.assignee = other.assignee
                if not merged.deadline and other.deadline:
                    merged.deadline = other.deadline
                merged.source_spans.extend(other.source_spans)
                merged.confidence = max(merged.confidence, other.confidence)
            deduplicated.append(merged)
    
    logger.info(f"GlobalDeduplicator: Reduced {len(merged_actions)} -> {len(deduplicated)} actions")
    
    return {**state, "merged_actions": deduplicated}


def action_finalizer_node(state: GraphState) -> GraphState:
    """
    [7] ACTION FINALIZER NODE
    Role: Enforce output schema
    - Fill nulls
    - Normalize verbs
    - Drop low-confidence hallucination risks
    - Sort chronologically
    """
    merged_actions = state.get("merged_actions", [])
    logger.info(f"ActionFinalizer: Finalizing {len(merged_actions)} actions")
    
    finalized = []
    
    for action in merged_actions:
        # Fill nulls
        if not action.description:
            continue  # Skip actions without description
        
        # Normalize verb
        verb = action.verb or "do"
        verb_normalizations = {
            "take care of": "fix",
            "handle": "fix",
            "deal with": "fix",
            "send": "send",
            "email": "send",
            "review": "review",
            "check": "review",
        }
        for pattern, normalized in verb_normalizations.items():
            if pattern.lower() in verb.lower():
                verb = normalized
                break
        
        # Drop low-confidence actions (< 0.3)
        if action.confidence and action.confidence < 0.3:
            logger.debug(f"ActionFinalizer: Dropping low-confidence action: {action.description}")
            continue
        
        # Ensure assignee defaults to speaker if missing
        assignee = action.assignee or action.speaker
        
        finalized_action = Action(
            description=action.description,
            assignee=assignee,
            deadline=action.deadline,
            speaker=action.speaker,
            verb=verb,
            object_text=action.object_text,
            confidence=action.confidence or 0.5,
            source_spans=list(set(action.source_spans)),  # Deduplicate spans
            meeting_window=action.meeting_window,
        )
        finalized.append(finalized_action)
    
    # Sort chronologically by meeting window
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


def should_continue(state: GraphState) -> str:
    """Determine if we should continue processing chunks."""
    chunks = state.get("chunks", [])
    chunk_index = state.get("chunk_index", 0)
    relevance_result = state.get("relevance_result", "")
    
    if relevance_result == "DONE":
        return "end"
    
    if relevance_result == "YES":
        return "extract"
    else:
        return "next_chunk"


def increment_chunk(state: GraphState) -> GraphState:
    """Move to next chunk."""
    chunk_index = state.get("chunk_index", 0)
    new_index = chunk_index + 1
    logger.info(f"IncrementChunk: Moving to chunk {new_index + 1}")
    return {**state, "chunk_index": new_index}
