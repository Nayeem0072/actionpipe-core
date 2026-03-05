# Action Extractor

Pipeline, node details, and performance for the action extractor (LangGraph workflow that extracts structured action items from meeting transcripts).

---

## Pipeline

The extractor pipeline is a **linear** LangGraph graph with no loops. After the segmenter chunks the transcript, all relevant chunks are extracted **concurrently** in the parallel extractor node. A single follow-up LLM call then resolves any cross-chunk semantic issues before the final deduplication and sorting passes.

```
┌─────────────────────┐
│      Segmenter      │  Splits transcript into 20-turn chunks       (no LLM)
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│  Parallel Extractor │  Keyword filter → concurrent LLM extraction  (LLM × N chunks)
│                     │
│  chunk 1 ──► LLM ─┐ │
│  chunk 2 ──► LLM ─┤ │  All chunks run at the same time.
│  chunk 3 ──► LLM ─┤ │  Wall time = max(chunk latency), not sum.
│  chunk N ──► LLM ─┘ │
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│ Evidence Normalizer │  ASR cleanup, dedup, action object creation  (no LLM)
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│Cross-chunk Resolver │  Semantic merge + cross-chunk pronoun resolve (1 LLM call)
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│ Global Deduplicator │  Text-similarity duplicate removal            (no LLM)
└──────────┬──────────┘
           │
┌──────────▼──────────┐
│   Action Finalizer  │  Schema enforcement, confidence filter, sort  (no LLM)
└──────────┬──────────┘
           │
          END
```

---

## Node Details

### 1. Segmenter *(no LLM)*

Parses the transcript into `Speaker: text` turns using a regex, then groups them into chunks of **20 turns** each. Larger chunks mean fewer LLM calls and keep most intra-conversation references (pronouns, topic callbacks) within a single chunk where the extractor can resolve them.

**Output:** list of text chunks, each containing up to 20 speaker turns.

---

### 2. Parallel Extractor *(LLM — concurrent)*

This node does two things in sequence:

**Step 1 — Keyword relevance filter (free):** Each chunk is scored by counting how many action-signal keywords it contains (`"will"`, `"should"`, `"need to"`, `"can you"`, `"deadline"`, `"i'll"`, `"schedule"`, etc.). A score of 0 means the chunk is purely conversational (greetings, small talk) and is skipped. This costs nothing and avoids burning LLM calls on filler content.

**Step 2 — Concurrent extraction:** All chunks that passed the filter are submitted to a `ThreadPoolExecutor` (capped at 6 concurrent workers). Each thread calls the LLM independently with the same structured extraction prompt, which instructs the model to:

- Extract every utterance with its speaker, intent, and resolved context
- Produce fully self-contained `action_item` descriptions (expand pronouns using surrounding turns within the chunk)
- Tag each action with 2–4 short subject keywords (`topic_tags`) for vocabulary-independent semantic matching
- Record what an unresolved cross-chunk reference appears to point to (`unresolved_reference`), when the context cannot be resolved within the current chunk alone
- Assign a confidence score to each action

Because threads run in parallel, total wall time is the latency of the **slowest** single chunk, not the sum of all chunks.

**Output:** combined, chunk-ordered list of `Segment` objects from all relevant chunks.

---

### 3. Evidence Normalizer *(no LLM)*

Cleans all segments and converts `action_item` segments into `Action` objects:

- **ASR noise removal** — strips filler words (`um`, `uh`, `er`, `ah`, `like`, `you know`)
- **Whitespace normalisation** — collapses multiple spaces
- **Cross-chunk deduplication** — drops exact-text-match duplicates from any chunk
- **Meta-action filtering** — drops utterances that acknowledge note-taking rather than committing to work (e.g. `"noted"`, `"writing that down"`, `"adding to list"`)
- **Verb normalisation** — maps informal phrases to canonical verbs (`"take care of"` → `"fix"`, `"gonna"` → `"will"`)
- **Action creation** — converts each surviving `action_item` segment into a typed `Action` object with `meeting_window`, `source_spans`, and confidence

---

### 4. Cross-chunk Resolver *(1 LLM call)*

Addresses two failure modes that chunk-isolated extraction cannot handle:

1. **Same task, different vocabulary** — `"handle the API gateway migration"` (chunk 1) and `"prepare migration plan with rollback"` (chunk 2) share few words but describe the same task. The text-similarity deduplicator would miss this; the resolver catches it using `topic_tags`.
2. **Cross-chunk pronoun resolution** — `"I'll do that"` in chunk N where `"that"` was introduced in chunk N-1.

The node formats all extracted actions into a compact prompt listing each action's index, chunk number, speaker, `topic_tags`, optional `unresolved_reference`, and description. A single LLM call returns:

- **`merge_groups`** — groups of action indices that represent the same real-world task (e.g. `[[0, 2]]`). For each group, the most specific (longest) description is kept as the representative; `assignee`, `deadline`, `topic_tags`, and `source_spans` are merged from all members.
- **`updates`** — field patches for individual actions: a rewritten self-contained description for vague references, or a missing `deadline`/`assignee` linked from a related action in another chunk.

**Skip condition:** automatically skipped when there is only 1 chunk or fewer than 2 actions — nothing to resolve.

**Fallback:** if the LLM call fails or returns an invalid structure, the action list passes through unchanged (same output as if the node did not exist).

---

### 5. Global Deduplicator *(no LLM)*

Merges actions that refer to the same real-world task across all chunks. Two actions are considered duplicates when all of the following are true:

- **Similar verb** — exact match or within a synonym group (`fix`/`handle`/`deal with`, `send`/`email`, `review`/`check`)
- **High description overlap** — ≥ 40% word overlap after removing stop words
- **Close meeting window** — within 3 chunks of each other

When merging a group, the representative is the action whose speaker is also the assignee (the person actually doing the work). Missing deadline or assignee fields are filled from other members of the group.

---

### 6. Action Finalizer *(no LLM)*

Enforces the output schema and drops low-quality results:

- Skips actions without a description
- Drops actions with confidence below 0.3 (likely hallucinations or noise)
- Defaults `assignee` to `speaker` if no assignee was extracted
- Normalises verbs to canonical forms
- Deduplicates `source_spans` within each action
- Sorts the final list chronologically by `meeting_window[0]`

---

## Performance

### Optimization impact (`input_very_small.txt`, 63 turns, `ACTIVE_PROVIDER=gemini_mixed`)

| Stage | Before (sequential) | After (parallel + resolver) |
|---|---|---|
| Chunks | 8 | 4 |
| LLM calls | 22 (sequential) | 4 concurrent + 1 resolver |
| Parallel extraction | ~80 s | ~18 s |
| Cross-chunk resolution | — | ~5 s |
| Total runtime | ~92 s | ~23 s |
| Actions extracted | 5 | 5+ (cross-chunk merges applied) |

### Scaling across transcript sizes (`ACTIVE_PROVIDER=gemini_mixed`)

| Transcript | Turns | Chunks | LLM calls | Extraction | Resolution | **Total** | Actions |
|---|---|---|---|---|---|---|---|
| `input_very_small.txt` | 63 | 4 | 4 + 1 | ~18 s | ~5 s | **~23 s** | 5 |
| `input_small.txt` | 99 | 5 | 5 + 1 | ~20 s | ~10 s | **~30 s** | 9 |
| `input.txt` | 130 | 7 (1 skipped) | 6 + 1 | ~21 s | ~5 s | **~27 s** | 9 |
| `input_large.txt` | 300 | 15 | 15 + 1 | ~49 s | ~19 s | **~68 s** | 33 |

The key observation is that total runtime scales only weakly with transcript length for the extraction phase — all chunks run in parallel so wall time is bounded by the **slowest single chunk**, not the sum. Going from 63 turns to 300 turns (nearly 5× more content) adds ~31 s in extraction. The cross-chunk resolver scales with the number of extracted actions; with 33 actions across 15 chunks, resolution grows to ~19 s compared to ~5–10 s for smaller transcripts.
