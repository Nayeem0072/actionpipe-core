---
name: Pipeline Efficiency Redesign
overview: The current pipeline's sequential, over-segmented design forces ~22 LLM calls for 63 turns. The redesign focuses on four targeted changes — larger chunks, rule-based gating, merging two LLM nodes into one, and async parallel extraction — to deliver a meaningfully faster pipeline that still scales to large transcripts.
todos: []
isProject: false
---

# Pipeline Efficiency Redesign

## Current Design Assessment

The conceptual architecture is **sound**: segment → filter → extract → normalize → resolve → deduplicate → finalize. The nodes are clean, debuggable, and the rule-based stages (evidence_normalizer, global_deduplicator, action_finalizer) are free and correct.

**The root problems are execution, not design:**

### Measured timing (input_very_small.txt — 63 turns, 92 seconds actual):

```
Node               Calls   Total Time   % of Runtime
─────────────────────────────────────────────────────
local_extractor      7       52.6s          57%
context_resolver     7       31.3s          34%
relevance_gate       8        8.0s           9%
─────────────────────────────────────────────────────
Total LLM calls:    22       91.9s
```

### Problem 1: Chunk size of 8 turns is too small

63 turns → 8 chunks → 22 LLM calls. This is the multiplier behind everything else. For a 500-turn transcript, this would produce 63 chunks and ~190 sequential LLM calls.

### Problem 2: LLM relevance gate fires on almost everything

7 of 8 chunks passed (87.5% pass rate). It correctly filtered one greetings chunk, but that chunk could be identified for free with a keyword heuristic. The gate adds ~1s × N_chunks with negligible filtering value.

### Problem 3: Context resolver fires even when there's nothing to resolve

4 of 7 context resolver calls produced 0 new actions. It's making a full Claude Haiku call (~4–7s) to return an empty result. The cost is wasted entirely.

### Problem 4: No parallelism — all sequential

Chunk extractions are independent of each other (local_extractor has no dependency on the previous chunk's result). There's no reason they must be sequential, yet the current LangGraph loop processes them one-by-one.

---

## Recommended Changes (in priority order)

### Change 1 — Increase chunk size: 8 → 20 turns

**File:** `[src/langgraph_nodes.py](src/langgraph_nodes.py)` — `segmenter_node`, hardcoded `chunk_size = 8`

```python
chunk_size = 20  # was 8
```

Impact: 63 turns → 3 chunks (was 8). All LLM call counts drop by ~2.6x. Larger context per chunk also improves extraction quality since action items and their resolution often span 12–16 turns.

---

### Change 2 — Replace LLM relevance gate with a rule-based scorer

**File:** `[src/langgraph_nodes.py](src/langgraph_nodes.py)` — `relevance_gate_node`

Replace the Gemini Flash Lite call with a fast keyword scoring function. Only fall back to LLM if the score is genuinely ambiguous (in a mid-range band).

```python
ACTION_KEYWORDS = [
    "will", "should", "need to", "needs to", "going to",
    "can you", "could you", "please", "follow up", "schedule",
    "by when", "deadline", "i'll", "we'll", "let's",
    "make sure", "track", "add to", "review", "fix", "update"
]

def _score_chunk_relevance(chunk_text: str) -> float:
    text = chunk_text.lower()
    return sum(1 for kw in ACTION_KEYWORDS if kw in text)

# Decision thresholds:
# score >= 2  → YES (skip LLM)
# score == 0  → NO  (skip LLM)
# score == 1  → ambiguous, call LLM
```

Impact: Saves ~1s × N_chunks with no quality loss. LLM gate only fires on genuinely ambiguous chunks.

---

### Change 3 — Merge local_extractor + context_resolver into one combined node

**Files:** `[src/langgraph_nodes.py](src/langgraph_nodes.py)`, `[src/langgraph_workflow.py](src/langgraph_workflow.py)`

The context_resolver currently receives the local_extractor's output and the accumulated action list, then produces updates. These can be done in a single LLM call with a unified prompt. The combined node receives the chunk text + a snapshot of prior actions and returns extracted + resolved candidates in one shot.

Impact: Reduces LLM calls from 2 per relevant chunk → 1. For the small input: 7+7=14 calls → 7 calls. Saves ~4–7s per chunk (the full context_resolver cost).

New graph flow after this change:

```
segmenter → relevance_gate → combined_extractor → evidence_normalizer → increment_chunk → (loop) → global_deduplicator → action_finalizer → END
```

---

### Change 4 — Async parallel chunk extraction (biggest win for large transcripts)

**Files:** `[src/langgraph_nodes.py](src/langgraph_nodes.py)`, `[src/langgraph_workflow.py](src/langgraph_workflow.py)`

This is the highest-leverage change for large transcripts. The current loop processes one chunk at a time. Since the local_extractor calls are **independent** (they only need the chunk text, not output from other chunks), they can all run concurrently.

**Why the current loop can't simply be parallelized:** The context_resolver needs `merged_actions` from previous chunks (it's stateful). But with Change 3, we've already merged extraction + local context resolution into one node, reducing this dependency to just a read-only snapshot of prior actions.

**Two-phase restructure:**

```
Phase 1 (parallel):  All chunk extractions run concurrently via asyncio.gather()
                     Each chunk gets a read-only snapshot of prior context (or empty on first pass)
                     
Phase 2 (sequential): global_deduplicator + action_finalizer resolve any remaining cross-chunk refs
```

Implementation approach — replace the sequential LangGraph loop with a single `parallel_extractor` node that fans out internally using `asyncio.gather()`:

```python
async def parallel_extractor_node(state: GraphState) -> GraphState:
    relevant_chunks = [
        (i, chunk) for i, chunk in enumerate(state["chunks"])
        if _score_chunk_relevance(chunk) > 0
    ]
    tasks = [_extract_single_chunk(chunk, idx) for idx, chunk in relevant_chunks]
    all_results = await asyncio.gather(*tasks)
    # flatten and return all candidates
    ...
```

New graph flow after this change:

```
segmenter → parallel_extractor → evidence_normalizer → global_deduplicator → action_finalizer → END
```

The per-chunk loop in LangGraph is replaced by a single node that processes all chunks concurrently. This eliminates the `relevance_gate`, `increment_chunk`, and `context_resolver` as separate graph nodes — their logic is absorbed into the combined parallel node.

---

## Expected Performance Gains


| Change                               | small input (63 turns) | large input (500 turns) |
| ------------------------------------ | ---------------------- | ----------------------- |
| Baseline (current)                   | ~92s                   | ~700s+                  |
| +Change 1 (chunk size 20)            | ~35s                   | ~270s                   |
| +Change 2 (rule-based gate)          | ~28s                   | ~215s                   |
| +Change 3 (merge extractor+resolver) | ~18s                   | ~140s                   |
| +Change 4 (parallel execution)       | **~10–12s**            | **~15–20s**             |


Change 4 is what makes this scale — large transcripts go from potentially 10+ minutes to under 20 seconds, because wall time becomes `max(single_chunk_time)` instead of `sum(all_chunk_times)`.

---

## What stays the same

- `segmenter_node` — structural chunking logic is good, just the chunk size changes
- `evidence_normalizer` — free, fast, and works well
- `global_deduplicator` — solid similarity logic, keep as-is
- `action_finalizer` — schema enforcement, keep as-is
- LLM provider config — Gemini Flash for extraction is a good choice; Claude Haiku for context resolver goes away with Change 3 (use one consistent provider)

