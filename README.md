# LangGraph Meeting Action Item Extractor + Normalizer

Extracts structured action items from raw meeting transcripts using a parallel LangGraph pipeline, then normalizes each action into a ready-to-execute tool call — email, Jira ticket, calendar event, or Notion document. Supports multiple LLM providers — run fully locally via Ollama, or use Gemini or Claude APIs for higher accuracy.

---

## Features

### Extractor

- Splits transcripts into 20-turn chunks that preserve conversational context
- Filters irrelevant chunks (greetings, small talk, audio glitches) with a free keyword scorer — no LLM cost
- Extracts action items, decisions, and suggestions from all relevant chunks **concurrently** (wall time = slowest single chunk, not the sum)
- Resolves pronouns and references within each chunk — descriptions are always self-contained
- **Semantically merges cross-chunk duplicates** and resolves vague references that span chunk boundaries (`"I'll handle that"` ↔ the task it refers to from a prior chunk)
- Cleans ASR noise (filler words, repeated phrases) and normalizes verbs
- Deduplicates actions across all chunks and produces a chronologically sorted final list
- Tags each action with an `action_category` hint (`communication`, `task`, `event`, `documentation`) used downstream by the normalizer

### Normalizer

- **Deadline normalization** — converts free-text deadlines to ISO 8601 dates: `"after the meeting"` → `2026-03-05`, `"March 10"` → `2026-03-10`, `"later"` → `null`
- **Verb upgrading** — replaces weak or colloquial verbs with precise, tool-ready ones: `"talk to"` → `notify`, `"circle back"` → `follow_up`, `"look into"` → `investigate`
- **Compound action splitting** — breaks multi-verb descriptions into atomic actions: `"Investigate flaky tests and fix them"` → two separate Jira tasks
- **Deduplication** — removes semantically equivalent actions using Jaccard similarity, same assignee, and same verb
- **Tool classification** — maps each action to the right tool: `send_email`, `create_jira_task`, `set_calendar`, `create_notion_doc`, `send_notification`, or `general_task`
- **Tool parameter extraction** — pulls structured, tool-ready parameters from the description (recipient, subject, priority, event time, etc.) using regex — no extra LLM calls
- **Hybrid approach** — rule-based dictionaries and regex patterns handle ~90% of cases; LLM is only called for genuinely ambiguous splits or unclassifiable actions

---

## Quickstart

```bash
# 1. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure your provider
cp .env.example .env       # then edit .env (see Configuration below)

# 4. Extract action items from a transcript
python run_langgraph.py input.txt            # → output.json

# 5. Normalize into tool-ready actions
python run_normalizer.py output.json         # → normalized_output.json
```

---

## Installation

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt
```

Dependencies:

| Package | Purpose |
|---|---|
| `langchain` | LLM chain abstraction |
| `langchain-openai` | Ollama (OpenAI-compatible) provider |
| `langchain-anthropic` | Claude provider |
| `langchain-google-genai` | Gemini provider |
| `langgraph` | Graph workflow engine |
| `pydantic` | Data models and structured output |
| `python-dotenv` | `.env` file loading |
| `python-dateutil` | Deadline parsing (`"March 10"` → `2026-03-10`) |

---

## Configuration

The project root `.env` file controls which provider is active and where API keys come from. Provider-specific model and generation settings live in `configs/`.

### Minimal `.env`

```env
# Pick one provider
ACTIVE_PROVIDER=gemini_mixed   # or: claude | ollama

# API keys (only the one your provider needs)
GOOGLE_API_KEY=AIza...
ANTHROPIC_API_KEY=sk-ant-...
```

### Supported providers

| `ACTIVE_PROVIDER` | Config file | Description |
|---|---|---|
| `gemini_mixed` | `configs/gemini_mixed.env` | Gemini Flash — fast, accurate, recommended |
| `claude` | `configs/claude.env` | Claude Haiku — strong reasoning, Anthropic API |
| `ollama` | `configs/ollama_glm.env` | Local inference via Ollama — no data leaves machine |

---

### Provider: `gemini_mixed` (recommended)

Uses Google's Gemini API for extraction. Fast and cost-effective.

```env
ACTIVE_PROVIDER=gemini_mixed
GOOGLE_API_KEY=AIza...
```

| Setting | Value |
|---|---|
| Model | `gemini-2.5-flash` |
| Temperature | 0.2 |
| Max tokens | 4096 |
| Timeout | 120 s |

---

### Provider: `claude`

Uses Anthropic's Claude API.

```env
ACTIVE_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-...
```

| Setting | Value |
|---|---|
| Model | `claude-haiku-4-5-20251001` |
| Temperature | 0.2 |
| Max tokens | 4096 |
| Timeout | 120 s |

---

### Provider: `ollama` (local)

Runs models locally. No data leaves your machine.

1. Install [Ollama](https://ollama.com) and pull the model:

```bash
ollama run glm-4.7-flash
```

2. Set in `.env`:

```env
ACTIVE_PROVIDER=ollama
LANGGRAPH_API_KEY=ollama   # any non-empty string; Ollama ignores it
```

---

### Adding a new provider

1. Create `configs/<name>.env` with `PROVIDER=<type>` (where type is `gemini`, `claude`, or `ollama`) and the per-node variables.
2. Register it in `src/langgraph_llm_config.py` (`_provider_env_map`).
3. Set `ACTIVE_PROVIDER=<name>` in `.env`.
4. Add any API key to `.env` (gitignored).

---

## Usage

### Extractor

**Default files** (`input.txt` → `output.json`):

```bash
python run_langgraph.py
```

**Custom input** (output still goes to `output.json`):

```bash
python run_langgraph.py my_transcript.txt
```

**Custom input and output:**

```bash
python run_langgraph.py my_transcript.txt my_output.json
```

**As a Python module:**

```bash
python -m src.langgraph_main my_transcript.txt my_output.json
```

**As a library:**

```python
from src.langgraph_workflow import extract_actions

actions = extract_actions(transcript_raw="<your transcript text>")
# returns a list of dicts, one per action item
```

### Input format

Plain text transcript with `Speaker: text` lines:

```
John: ok so main thing today is the phoenix project timeline
Sara: should we send the client an update email today
John: yes good point — ill draft it after this
Priya: can you include the phased delivery plan
```

JSON input is also supported if the file contains a `transcript_raw` field.

---

### Normalizer

The normalizer reads an extractor output file (JSON array) and writes a `normalized_output.json` with each action mapped to a specific tool.

**Default** (`output.json` → `normalized_output.json`):

```bash
python run_normalizer.py
```

**Custom input and output:**

```bash
python run_normalizer.py output.json result.json
```

**With an explicit meeting date** (used for relative deadline resolution — defaults to today):

```bash
python run_normalizer.py output.json result.json --meeting-date 2026-03-05
```

**As a library:**

```python
from src.action_normalizer_workflow import normalize_actions

# raw_actions is the list returned by extract_actions() or loaded from output.json
normalized = normalize_actions(raw_actions, meeting_date="2026-03-05")
# returns a list of NormalizedAction dicts
```

**End-to-end pipeline in Python:**

```python
from src.langgraph_workflow import extract_actions
from src.action_normalizer_workflow import normalize_actions

actions = extract_actions(transcript_raw=open("meeting.txt").read())
normalized = normalize_actions(actions, meeting_date="2026-03-05")
```

The normalizer prints a summary table to stdout on completion:

```
──────────────────────────────────────────────────────────────────────────────────────────
#    TOOL                    VERB            DEADLINE      ASSIGNEE    DESCRIPTION
──────────────────────────────────────────────────────────────────────────────────────────
1    create_jira_task        investigate     —             John        Investigate flaky tests (split)
2    create_jira_task        resolve         —             John        Resolve flaky tests issue (split)
3    send_email              draft           2026-03-05    John        Draft update email to client...
4    set_calendar            schedule        2026-03-10    John        Schedule bug bash session...
5    send_notification       notify          —             John        Talk to finance about debug...
──────────────────────────────────────────────────────────────────────────────────────────
```

---

## Pipelines

### Extractor pipeline

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

### Normalizer pipeline

A second independent LangGraph pipeline that consumes the extractor's output. Also fully linear with no loops.

```
┌────────────────────────┐
│  Deadline Normalizer   │  ISO date conversion (rule-based regex + dateutil)  (no LLM)
└───────────┬────────────┘
            │
┌───────────▼────────────┐
│    Verb Enricher       │  Extract + upgrade verbs via dictionary             (no LLM*)
└───────────┬────────────┘
            │
┌───────────▼────────────┐
│   Action Splitter      │  Compound detection (rule-based) + split            (LLM per compound)
└───────────┬────────────┘
            │
┌───────────▼────────────┐
│    Deduplicator        │  Jaccard similarity dedup                           (no LLM)
└───────────┬────────────┘
            │
┌───────────▼────────────┐
│   Tool Classifier      │  Verb + category + keyword → ToolType + params      (no LLM*)
└───────────┬────────────┘
            │
           END

* LLM called only when rule-based logic cannot determine the answer (rare)
```

---

## Node Details

### Extractor nodes

#### 1. Segmenter *(no LLM)*

Parses the transcript into `Speaker: text` turns using a regex, then groups them into chunks of **20 turns** each. Larger chunks mean fewer LLM calls and keep most intra-conversation references (pronouns, topic callbacks) within a single chunk where the extractor can resolve them.

**Output:** list of text chunks, each containing up to 20 speaker turns.

---

#### 2. Parallel Extractor *(LLM — concurrent)*

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

#### 3. Evidence Normalizer *(no LLM)*

Cleans all segments and converts `action_item` segments into `Action` objects:

- **ASR noise removal** — strips filler words (`um`, `uh`, `er`, `ah`, `like`, `you know`)
- **Whitespace normalisation** — collapses multiple spaces
- **Cross-chunk deduplication** — drops exact-text-match duplicates from any chunk
- **Meta-action filtering** — drops utterances that acknowledge note-taking rather than committing to work (e.g. `"noted"`, `"writing that down"`, `"adding to list"`)
- **Verb normalisation** — maps informal phrases to canonical verbs (`"take care of"` → `"fix"`, `"gonna"` → `"will"`)
- **Action creation** — converts each surviving `action_item` segment into a typed `Action` object with `meeting_window`, `source_spans`, and confidence

---

#### 4. Cross-chunk Resolver *(1 LLM call)*

Addresses two failure modes that chunk-isolated extraction cannot handle:

1. **Same task, different vocabulary** — `"handle the API gateway migration"` (chunk 1) and `"prepare migration plan with rollback"` (chunk 2) share few words but describe the same task. The text-similarity deduplicator would miss this; the resolver catches it using `topic_tags`.
2. **Cross-chunk pronoun resolution** — `"I'll do that"` in chunk N where `"that"` was introduced in chunk N-1.

The node formats all extracted actions into a compact prompt listing each action's index, chunk number, speaker, `topic_tags`, optional `unresolved_reference`, and description. A single LLM call returns:

- **`merge_groups`** — groups of action indices that represent the same real-world task (e.g. `[[0, 2]]`). For each group, the most specific (longest) description is kept as the representative; `assignee`, `deadline`, `topic_tags`, and `source_spans` are merged from all members.
- **`updates`** — field patches for individual actions: a rewritten self-contained description for vague references, or a missing `deadline`/`assignee` linked from a related action in another chunk.

**Skip condition:** automatically skipped when there is only 1 chunk or fewer than 2 actions — nothing to resolve.

**Fallback:** if the LLM call fails or returns an invalid structure, the action list passes through unchanged (same output as if the node did not exist).

---

#### 5. Global Deduplicator *(no LLM)*

Merges actions that refer to the same real-world task across all chunks. Two actions are considered duplicates when all of the following are true:

- **Similar verb** — exact match or within a synonym group (`fix`/`handle`/`deal with`, `send`/`email`, `review`/`check`)
- **High description overlap** — ≥ 40% word overlap after removing stop words
- **Close meeting window** — within 3 chunks of each other

When merging a group, the representative is the action whose speaker is also the assignee (the person actually doing the work). Missing deadline or assignee fields are filled from other members of the group.

---

#### 6. Action Finalizer *(no LLM)*

Enforces the output schema and drops low-quality results:

- Skips actions without a description
- Drops actions with confidence below 0.3 (likely hallucinations or noise)
- Defaults `assignee` to `speaker` if no assignee was extracted
- Normalises verbs to canonical forms
- Deduplicates `source_spans` within each action
- Sorts the final list chronologically by `meeting_window[0]`

---

### Normalizer nodes

#### 1. Deadline Normalizer *(no LLM)*

Converts the free-text `deadline` field from the extractor into an ISO 8601 date string or `null`. The reference date defaults to today and can be overridden with `--meeting-date`.

| Raw deadline | Normalized |
|---|---|
| `"after the meeting"` | `"2026-03-05"` (today) |
| `"later"` | `null` |
| `"March 10"` | `"2026-03-10"` |
| `"next week"` | `"2026-03-09"` (next Monday) |
| `"end of day"` / `"ASAP"` | `"2026-03-05"` (today) |
| `"tomorrow"` | `"2026-03-06"` |
| `"end of week"` | `"2026-03-06"` (Friday) |
| `"end of month"` | `"2026-03-31"` |
| `null` | `null` |

Uses `dateutil.parser` for explicit date strings (`"March 10 at 2 pm"`, `"10/3"`, etc.) with a year-advancement guard so past dates are interpreted as next year.

Also converts each `Action` dict from the extractor into a `NormalizedAction` object, initialising `tool_type` to `general_task` as a placeholder for the classifier.

---

#### 2. Verb Enricher *(no LLM)*

Extracts the primary action verb from the description and upgrades weak or colloquial verbs to precise, tool-friendly ones.

**Step 1 — Verb extraction (rule-based):**

Matches the longest applicable verb phrase from the start of the description using a priority-ordered list. Handles descriptions that start with a person's name (e.g. `"John will talk to finance..."`) by detecting `"Name will/to/needs to [verb]"` patterns and skipping to the actual verb.

**Step 2 — Verb upgrade dictionary:**

| Raw verb | Upgraded |
|---|---|
| `talk to`, `speak with`, `tell`, `reach out` | `notify` |
| `circle back`, `follow through` | `follow_up` |
| `look into` | `investigate` |
| `check on`, `check in`, `check` | `review` |
| `check with` | `notify` |
| `take care of`, `deal with` | `resolve` |

**Step 3 — LLM fallback** (only when the description yields no recognisable verb after steps 1 and 2, which is rare).

---

#### 3. Action Splitter *(LLM for compound candidates only)*

Detects and splits descriptions that contain two or more independently executable actions.

**Rule-based detection** — flags a description as a compound candidate when it contains:
- A conjunction keyword (`and`, `as well as`, `additionally`)
- Two or more distinct action verbs from the known verb set

**LLM split decision** — only compound candidates are sent to the LLM with a tight prompt that includes canonical examples:

- `"Investigate flaky tests and fix them"` → `["Investigate flaky tests", "Fix flaky tests"]` ✓ split
- `"Create and track a task for fixing alerts"` → single Jira ticket ✗ no split

Each split action inherits the parent's assignee, deadline, confidence, and `source_spans`, and carries a `parent_id` linking back to the original compound action.

---

#### 4. Deduplicator *(no LLM)*

Removes actions that describe the same real-world task. Two actions are considered duplicates when **all** of:

- Same `assignee` (or at least one is null)
- Same `verb`
- Description Jaccard similarity ≥ 0.6 (after removing stop words)

The representative is the highest-confidence action; `source_spans` are merged from all duplicates.

---

#### 5. Tool Classifier *(no LLM)*

Classifies each action into a `ToolType` and extracts tool-specific parameters. Three signals are checked in order:

1. **Verb → tool map** — most reliable; e.g. `draft` → `send_email`, `schedule` → `set_calendar`, `investigate` → `create_jira_task`
2. **`action_category` hint** — propagated from the extractor; e.g. `"event"` → `set_calendar`
3. **Keyword scan of description** — catches cases where the verb alone is ambiguous

After classification, regex-based extractors pull tool-specific parameters from the description:

| Tool | Extracted parameters |
|---|---|
| `send_email` | `to`, `subject_hint`, `body_hint` |
| `create_jira_task` | `title`, `assignee`, `priority` (from confidence), `due_date`, `labels` |
| `set_calendar` | `event_name`, `date`, `time`, `participants` |
| `send_notification` | `recipient`, `channel` (default: `slack`), `message_hint` |
| `create_notion_doc` | `page_title`, `content_hint`, `template` |

An LLM batch call is made only for actions that remain as `general_task` after all three rule-based signals fail — typically fewer than one per run.

---

## Output

### Extractor output

Written to `output.json` (or the path you specify). A JSON array, one object per action item.

| Field | Type | Description |
|---|---|---|
| `description` | `string` | Full, self-contained description of what needs to be done |
| `assignee` | `string \| null` | Person responsible (defaults to speaker if not specified) |
| `deadline` | `string \| null` | When it is due (`"end of month"`, `"March 15"`) or `null` |
| `speaker` | `string` | Who raised the action in the meeting |
| `verb` | `string` | Normalised action verb (`"fix"`, `"send"`, `"review"`) |
| `confidence` | `float` | Extraction confidence score (0.0–1.0) |
| `source_spans` | `string[]` | Segment IDs the action was derived from |
| `meeting_window` | `[int, int]` | Chunk range `[start, end]` where the action was discussed |
| `topic_tags` | `string[]` | Subject keywords used for cross-chunk semantic matching (e.g. `["client", "email", "scope"]`) |
| `unresolved_reference` | `string \| null` | Short phrase describing a cross-chunk reference that could not be resolved during extraction; `null` when fully self-contained |
| `action_category` | `string \| null` | Category hint for the normalizer: `communication`, `task`, `event`, `documentation`, or `other` |

Example:

```json
[
  {
    "description": "Draft email to Client Delta to reset expectations on the delivery date, including phased delivery plan and scope change impact",
    "assignee": "John",
    "deadline": "after this meeting",
    "speaker": "John",
    "verb": "draft",
    "confidence": 0.9,
    "source_spans": ["a3f1c2d4e5b6"],
    "meeting_window": [1, 1],
    "topic_tags": ["client", "email", "scope"],
    "unresolved_reference": null,
    "action_category": "communication"
  },
  {
    "description": "Schedule bug bash session before release",
    "assignee": "John",
    "deadline": "March 10",
    "speaker": "John",
    "verb": "schedule",
    "confidence": 0.85,
    "source_spans": ["f7e2b1c9a032", "c1a4d7e82b91"],
    "meeting_window": [1, 2],
    "topic_tags": ["bug-bash", "testing", "release"],
    "unresolved_reference": null,
    "action_category": "event"
  }
]
```

An execution log is written to `output_log.txt` with per-node timing, segment counts, and the final action list.

---

### Normalizer output

Written to `normalized_output.json` (or the path you specify). A JSON array of `NormalizedAction` objects.

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Short unique ID for this action (8-char hex) |
| `description` | `string` | Clean, atomic description (may differ from extractor if split or rewritten) |
| `assignee` | `string \| null` | Who is responsible |
| `raw_deadline` | `string \| null` | Original deadline string from the extractor |
| `normalized_deadline` | `string \| null` | ISO 8601 date (`YYYY-MM-DD`) or `null` |
| `speaker` | `string` | Who mentioned this action in the meeting |
| `verb` | `string` | Upgraded, tool-ready verb (`"notify"`, `"investigate"`, `"schedule"`) |
| `confidence` | `float` | Confidence score 0.0–1.0 |
| `tool_type` | `string` | One of: `send_email`, `create_jira_task`, `set_calendar`, `create_notion_doc`, `send_notification`, `general_task` |
| `tool_params` | `object` | Tool-specific parameters extracted from the description |
| `source_spans` | `string[]` | Span IDs from the original transcript |
| `parent_id` | `string \| null` | ID of the compound action this was split from; `null` if not a split |
| `meeting_window` | `[int, int]` | Chunk range from the extractor |
| `action_category` | `string \| null` | Category hint propagated from the extractor |
| `topic_tags` | `string[]` | Subject keywords propagated from the extractor |

Example (two actions from a single compound split, plus a classification example):

```json
[
  {
    "id": "85efe08c",
    "description": "Investigate flaky tests",
    "assignee": "John",
    "raw_deadline": "later",
    "normalized_deadline": null,
    "speaker": "John",
    "verb": "investigate",
    "confidence": 0.85,
    "tool_type": "create_jira_task",
    "tool_params": {
      "title": "Investigate flaky tests",
      "assignee": "John",
      "priority": "medium",
      "due_date": null,
      "labels": ["tests", "flaky"]
    },
    "source_spans": ["cbefa1ccbd15"],
    "parent_id": "d5e590d8",
    "meeting_window": [2, 2],
    "action_category": "task",
    "topic_tags": ["tests", "flaky"]
  },
  {
    "id": "2ff4df27",
    "description": "Resolve flaky tests issue",
    "assignee": "John",
    "raw_deadline": "later",
    "normalized_deadline": null,
    "speaker": "John",
    "verb": "resolve",
    "confidence": 0.85,
    "tool_type": "create_jira_task",
    "tool_params": {
      "title": "Resolve flaky tests issue",
      "assignee": "John",
      "priority": "medium",
      "due_date": null,
      "labels": ["tests", "flaky"]
    },
    "source_spans": ["cbefa1ccbd15"],
    "parent_id": "d5e590d8",
    "meeting_window": [2, 2],
    "action_category": "task",
    "topic_tags": ["tests", "flaky"]
  },
  {
    "id": "812f7cd3",
    "description": "Draft update email to client to reset expectations",
    "assignee": "John",
    "raw_deadline": "after the meeting",
    "normalized_deadline": "2026-03-05",
    "speaker": "John",
    "verb": "draft",
    "confidence": 0.85,
    "tool_type": "send_email",
    "tool_params": {
      "to": "client",
      "subject_hint": "Draft update email to client to reset expectations",
      "body_hint": "Draft update email to client to reset expectations"
    },
    "source_spans": ["f5ddcca3181f"],
    "parent_id": null,
    "meeting_window": [3, 3],
    "action_category": "communication",
    "topic_tags": ["client", "email", "scope"]
  }
]
```

An execution log is written to `normalizer_log.txt`.

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

---

## Project Structure

```
agent-ai/
├── src/
│   ├── __init__.py
│   │
│   │   ── Extractor ──────────────────────────────────────────────────────
│   ├── langgraph_main.py              # CLI entry point (extractor)
│   ├── langgraph_workflow.py          # Extractor graph + extract_actions()
│   ├── langgraph_nodes.py             # All extractor node implementations
│   ├── langgraph_state.py             # Extractor graph state (TypedDict)
│   ├── langgraph_models.py            # Pydantic models: Segment, Action, ActionDetails
│   ├── langgraph_llm_config.py        # Per-node LLM config (loaded from .env + configs/)
│   │
│   │   ── Normalizer ────────────────────────────────────────────────────
│   ├── action_normalizer_workflow.py  # Normalizer graph + normalize_actions()
│   ├── action_normalizer_nodes.py     # All normalizer node implementations
│   ├── action_normalizer_state.py     # Normalizer graph state (TypedDict)
│   ├── action_normalizer_models.py    # Pydantic models: NormalizedAction, ToolType
│   └── action_normalizer_data.py      # Rule-based data: verb dict, tool map, patterns
│
├── configs/
│   ├── gemini_mixed.env               # Gemini Flash provider config
│   ├── claude.env                     # Claude Haiku provider config
│   └── ollama_glm.env                 # Ollama local provider config
├── tests/
│   ├── test_langchain_to_llm.py
│   └── test_langchain_to_llm_standalone.py
├── docs/
├── run_langgraph.py                   # Extractor runner (wraps src.langgraph_main)
├── run_normalizer.py                  # Normalizer runner with summary table
├── input.txt                          # Default input transcript
├── input_very_small.txt               # Small test transcript (63 turns)
├── input_small.txt                    # Medium test transcript
├── output.json                        # Extractor output (generated on run)
├── normalized_output.json             # Normalizer output (generated on run)
├── output_log.txt                     # Extractor execution log
├── normalizer_log.txt                 # Normalizer execution log
├── .env                               # API keys and ACTIVE_PROVIDER (gitignored)
└── requirements.txt
```

---

## Requirements

- Python 3.10+
- **`gemini_mixed` provider:** `GOOGLE_API_KEY` and internet access
- **`claude` provider:** `ANTHROPIC_API_KEY` and internet access
- **`ollama` provider:** Ollama running locally with `glm-4.7-flash` pulled (or any OpenAI-compatible model at `http://localhost:11434/v1`)
