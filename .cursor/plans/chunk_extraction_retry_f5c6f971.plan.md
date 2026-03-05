---
name: Chunk extraction retry
overview: Add a retry mechanism to _extract_single_chunk so that a suspiciously low-yield response triggers a second attempt, using the chunk's relevance score (already computed) as a guard to avoid wasting retries on legitimately quiet chunks.
todos:
  - id: add-constants
    content: Add _MIN_SEGMENTS_PER_TURN_RATIO, _HIGH_RELEVANCE_SCORE_THRESHOLD, and _MAX_EXTRACTION_RETRIES constants near _MAX_PARALLEL_CHUNKS in langgraph_nodes.py
    status: completed
  - id: pass-score
    content: Pass the relevance_score into _extract_single_chunk alongside chunk and chunk_index so the retry guard can use it
    status: completed
  - id: extract-helper
    content: Extract the segment-building loop (lines 168-200) into a _parse_segments(result, chunk_index) helper function
    status: completed
  - id: retry-loop
    content: Wrap chain.invoke in a retry loop inside _extract_single_chunk, gating retries on both low yield AND high relevance score
    status: completed
  - id: anomaly-log
    content: Add post-hoc low-yield warning in parallel_extractor_node after all futures complete
    status: completed
isProject: false
---

# Chunk Extraction Retry

## The problem

In `[src/langgraph_nodes.py](src/langgraph_nodes.py)`, `_extract_single_chunk` has one error path: if the LLM call raises an exception it returns `[]`. But the observed failure mode is different — the call succeeds but returns a **partially empty response**:

```
WARNING Extractor: Chunk 3 segment 2 has empty text, skipping
INFO    ParallelExtractor: Chunk 3 completed, 2 segments
```

The LLM returned a valid `_SegmentExtraction` object, but most segments had empty `text` fields. The current code skips them one by one and silently accepts the 2-segment result, losing the email draft and bug bash action items entirely. There is no retry and no alert that a chunk is suspiciously under-extracted.

## Root cause

`with_structured_output` on Gemini (and occasionally other providers) can return a response where the JSON is technically valid but truncated mid-list — segments after the truncation point have empty or null fields. The caller has no way to distinguish a genuinely short chunk from a truncated one.

## Fix: detect low-yield AND high-relevance, then retry

The key insight is that the relevance score is already computed for every chunk inside `parallel_extractor_node` before the LLM call. It measures how many action-signal keywords the chunk contains. A chunk with a high score that returns very few segments is almost certainly truncated. A chunk with a low score that returns few segments is probably a quiet stretch of conversation — correct behaviour, no retry needed.

### 1. Constants

```python
_MIN_SEGMENTS_PER_TURN_RATIO = 1 / 5    # at least 1 segment per 5 turns
_HIGH_RELEVANCE_SCORE_THRESHOLD = 3     # score ≥ 3 means the chunk looks substantive
_MAX_EXTRACTION_RETRIES = 2
```

### 2. Pass `relevance_score` into `_extract_single_chunk`

Change the signature to:

```python
def _extract_single_chunk(chunk: str, chunk_index: int, relevance_score: int) -> List[Segment]:
```

In `parallel_extractor_node`, the relevance score is already available from the filter step (currently thrown away after the ≥ 1 check). Store and forward it:

```python
relevant = [
    (i, chunk, _score_chunk_relevance(chunk)) for i, chunk in enumerate(chunks)
    if _score_chunk_relevance(chunk) >= 1   # or compute once, reuse
]
# ...
executor.submit(_extract_single_chunk, chunk, idx, score)
```

### 3. Retry condition: low yield AND high relevance

```python
def _extract_single_chunk(chunk: str, chunk_index: int, relevance_score: int) -> List[Segment]:
    # ...build llm, chain, prompt as before...

    turn_count = chunk.count("\n\n") + 1
    min_expected = max(1, int(turn_count * _MIN_SEGMENTS_PER_TURN_RATIO))
    should_retry_on_low_yield = relevance_score >= _HIGH_RELEVANCE_SCORE_THRESHOLD

    best_segments: List[Segment] = []
    for attempt in range(1, _MAX_EXTRACTION_RETRIES + 2):  # 1 original + up to 2 retries
        try:
            result = chain.invoke({"chunk": chunk})
        except Exception as e:
            logger.error("Extractor: Chunk %d attempt %d failed: %s", chunk_index + 1, attempt, e)
            if attempt <= _MAX_EXTRACTION_RETRIES:
                continue
            return best_segments

        segments = _parse_segments(result, chunk_index)

        # Keep the best partial result seen so far
        if len(segments) > len(best_segments):
            best_segments = segments

        yield_ok = len(segments) >= min_expected
        retries_left = attempt <= _MAX_EXTRACTION_RETRIES

        if yield_ok or not should_retry_on_low_yield or not retries_left:
            if not yield_ok:
                logger.warning(
                    "Extractor: Chunk %d (relevance=%d) yielded %d segments "
                    "(expected ≥ %d) after %d attempt(s) — using best result",
                    chunk_index + 1, relevance_score, len(best_segments), min_expected, attempt,
                )
            return best_segments

        logger.warning(
            "Extractor: Chunk %d (relevance=%d) attempt %d yielded only %d segments "
            "(expected ≥ %d) — retrying",
            chunk_index + 1, relevance_score, attempt, len(segments), min_expected,
        )

    return best_segments
```

**What this means in practice:**


| Chunk type                                      | Relevance score | Yield          | Retry?                 |
| ----------------------------------------------- | --------------- | -------------- | ---------------------- |
| Substantive meeting content, truncated response | ≥ 3             | < min_expected | Yes — up to 2 retries  |
| Extended social chat with one keyword hit       | 1–2             | < min_expected | No — accepted as-is    |
| Any chunk                                       | any             | ≥ min_expected | No — passes first time |


### 4. Extract `_parse_segments` helper

The segment-building loop (lines 168–200 in `[src/langgraph_nodes.py](src/langgraph_nodes.py)`) moves into a `_parse_segments(result, chunk_index) -> List[Segment]` helper so it can be called on each attempt without code duplication.

### 5. Post-hoc anomaly log in `parallel_extractor_node`

After all futures complete, log a warning for any chunk whose segment count is disproportionately low compared to the average — a final catch for failures that survived all retries.

```python
if chunk_segment_map:
    avg = sum(len(s) for s in chunk_segment_map.values()) / len(chunk_segment_map)
    for idx, segs in chunk_segment_map.items():
        if len(segs) < avg * 0.3:
            logger.warning(
                "ParallelExtractor: Chunk %d has only %d segments vs avg %.1f "
                "— may be under-extracted even after retries",
                idx + 1, len(segs), avg,
            )
```

## Files to change

- `[src/langgraph_nodes.py](src/langgraph_nodes.py)` — only file that changes:
  - Add `_MIN_SEGMENTS_PER_TURN_RATIO`, `_HIGH_RELEVANCE_SCORE_THRESHOLD`, and `_MAX_EXTRACTION_RETRIES` constants
  - Store and forward `relevance_score` from the filter step to `_extract_single_chunk`
  - Extract `_parse_segments()` helper from `_extract_single_chunk`
  - Wrap `chain.invoke` in a retry loop gated on both low yield AND high relevance
  - Add post-hoc anomaly log in `parallel_extractor_node`

## What this does NOT change

- No prompt changes
- No model or config changes
- No impact on the happy path (well-formed responses pass through on the first attempt)
- No unnecessary retries on quiet/social chunks (relevance score guard prevents this)
- No performance impact on normal runs

