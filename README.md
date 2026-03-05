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
python run_extractor.py input/input.txt      # → output/output.json

# 5. Normalize into tool-ready actions
python run_normalizer.py                     # → output/normalized_output.json
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

**Default files** (`input/input.txt` → `output/output.json`):

```bash
python run_extractor.py
```

**Custom input** (output still goes to `output/output.json`):

```bash
python run_extractor.py my_transcript.txt
```

**Custom input and output:**

```bash
python run_extractor.py my_transcript.txt my_output.json
```

**As a Python module:**

```bash
python -m src.action_extractor.main my_transcript.txt my_output.json
```

**As a library:**

```python
from src.action_extractor.workflow import extract_actions

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

The normalizer reads an extractor output file (JSON array) and writes a `output/normalized_output.json` with each action mapped to a specific tool.

**Default** (`output/output.json` → `output/normalized_output.json`):

```bash
python run_normalizer.py
```

**Custom input and output:**

```bash
python run_normalizer.py output/output.json result.json
```

**With an explicit meeting date** (used for relative deadline resolution — defaults to today):

```bash
python run_normalizer.py output/output.json result.json --meeting-date 2026-03-05
```

**As a library:**

```python
from src.action_normalizer.workflow import normalize_actions

# raw_actions is the list returned by extract_actions() or loaded from output/output.json
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

## Pipelines, node details, and performance

Detailed pipeline diagrams, node-by-node descriptions, and performance notes are in separate docs:

- **[Action Extractor](docs/action_extractor.md)** — pipeline, extractor nodes (Segmenter → Action Finalizer), optimization impact, scaling
- **[Action Normalizer](docs/action_normalizer.md)** — pipeline, normalizer nodes (Deadline Normalizer → Tool Classifier), performance notes

---

## Output

### Extractor output

Written to `output/output.json` (or the path you specify). A JSON array, one object per action item.

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

An execution log is written to `output/output_log.txt` with per-node timing, segment counts, and the final action list.

---

### Normalizer output

Written to `output/normalized_output.json` (or the path you specify). A JSON array of `NormalizedAction` objects.

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

An execution log is written to `output/normalizer_log.txt`.

---

## Project Structure

```
agent-ai/
├── src/
│   ├── __init__.py
│   │
│   ├── action_extractor/              # ── Extractor ──────────────────────
│   │   ├── __init__.py
│   │   ├── main.py                    # CLI entry point
│   │   ├── workflow.py                # Extractor graph + extract_actions()
│   │   ├── nodes.py                   # All extractor node implementations
│   │   ├── state.py                   # Extractor graph state (TypedDict)
│   │   ├── models.py                  # Pydantic models: Segment, Action, ActionDetails
│   │   └── llm_config.py              # Per-node LLM config (loaded from .env + configs/)
│   │
│   └── action_normalizer/             # ── Normalizer ──────────────────────
│       ├── __init__.py
│       ├── workflow.py                # Normalizer graph + normalize_actions()
│       ├── nodes.py                   # All normalizer node implementations
│       ├── state.py                   # Normalizer graph state (TypedDict)
│       ├── models.py                  # Pydantic models: NormalizedAction, ToolType
│       └── data.py                    # Rule-based data: verb dict, tool map, patterns
│
├── input/
│   ├── input.txt                      # Default input transcript
│   ├── input_very_small.txt           # Small test transcript (63 turns)
│   ├── input_small.txt                # Medium test transcript
│   ├── input_large.txt                # Large test transcript
│   └── real_input.txt                 # Real meeting transcript
├── output/                            # Generated on run (gitignored)
│   ├── output.json                    # Extractor output
│   ├── normalized_output.json         # Normalizer output
│   ├── output_log.txt                 # Extractor execution log
│   └── normalizer_log.txt             # Normalizer execution log
├── configs/
│   ├── gemini_mixed.env               # Gemini Flash provider config
│   ├── claude.env                     # Claude Haiku provider config
│   └── ollama_glm.env                 # Ollama local provider config
├── tests/
│   ├── test_langchain_to_llm.py
│   └── test_langchain_to_llm_standalone.py
├── docs/
│   ├── action_extractor.md            # Extractor pipeline, nodes, performance
│   └── action_normalizer.md           # Normalizer pipeline, nodes, performance
├── run_extractor.py                   # Extractor runner (wraps src.action_extractor.main)
├── run_normalizer.py                  # Normalizer runner with summary table
├── .env                               # API keys and ACTIVE_PROVIDER (gitignored)
└── requirements.txt
```

---

## Requirements

- Python 3.10+
- **`gemini_mixed` provider:** `GOOGLE_API_KEY` and internet access
- **`claude` provider:** `ANTHROPIC_API_KEY` and internet access
- **`ollama` provider:** Ollama running locally with `glm-4.7-flash` pulled (or any OpenAI-compatible model at `http://localhost:11434/v1`)
