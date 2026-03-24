# Agent AI (ActionPipe)

Extract structured action items from meeting transcripts, normalize them into tool-ready payloads, and execute through MCP integrations (email, calendar, Slack, Jira, Notion) with safe dry-run defaults.

Currently Implemented: Jira (create and update issues) and Slack (DM and Public Channel send message).

## Table of Contents

- [What It Does](#what-it-does)
- [Core Capabilities](#core-capabilities)
- [Architecture and Runtime](#architecture-and-runtime)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [CLI and Library Usage](#cli-and-library-usage)
- [Project Structure](#project-structure)
- [Safety and Execution Controls](#safety-and-execution-controls)
- [Documentation Links](#documentation-links)

---

## What It Does

ActionPipe processes meeting transcripts through three stages:

1. **Extractor**: pulls structured actions from transcript chunks (parallelized).
2. **Normalizer**: converts actions into tool-ready, typed payloads.
3. **Executor**: resolves real contacts and dispatches MCP tool calls (dry-run by default).

The same flow is available through CLI and API:

- **CLI mode** for local file-based processing.
- **API mode** for uploaded transcripts, async worker execution, and real-time progress via SSE.
- **Worker mode** for resilient task execution with retries and checkpoint resume.

---

## Core Capabilities

### Extractor (Stage 1)

- 20-turn chunking with keyword relevance filtering (skip low-signal chunks).
- Parallel chunk extraction (`ThreadPoolExecutor`) for lower wall time.
- Cross-chunk semantic resolver for duplicate merge and vague reference fixes.
- Post-processing: ASR cleanup, global deduplication, schema finalization, confidence filtering.
- Structured output includes fields such as `meeting_window`, `source_spans`, `topic_tags`, `unresolved_reference`, and confidence.

### Normalizer (Stage 2)

- Rule-first deadline normalization to ISO dates.
- Verb enrichment/upgrades (`talk to` -> `notify`, `look into` -> `investigate`, etc.).
- Compound action splitting (LLM only for likely compounds).
- Tool classification + regex parameter extraction for:
  - `send_email`
  - `create_jira_task`
  - `set_calendar`
  - `create_notion_doc`
  - `send_notification`
  - `general_task`
- Deduplication uses semantic similarity with assignee/verb checks to reduce duplicate work items.

### Executor (Stage 3)

- Contact resolution against relation graph (`src/relation_graph/contacts.json`).
- Tool routing through MCP dispatcher (`mcp_config.json`).
- Dry-run safe mode by default; live mode via `--live`.
- Enrichment examples: email targets, calendar participants, Slack channels, Jira assignee, Notion workspace.
- Execution results include status (`success`, `dry_run`, `skipped`, `error`) and captured responses/errors.

## Key Problems and How the System Handles Them

### Pipeline Efficiency and LLM Cost


| Problem                                                 | How it is handled                                                                                            |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| Sequential chunk extraction makes runtime scale poorly. | Relevant chunks are extracted concurrently; wall time is driven by the slowest chunk, not total chunk count. |
| Too many small chunks increase LLM calls.               | Transcript is chunked into 20-turn windows to reduce overhead while preserving local context.                |
| LLM relevance checks add latency and cost.              | A free keyword relevance scorer filters low-signal chunks before extraction.                                 |
| Cross-chunk context can cause many extra calls.         | A single cross-chunk resolver pass runs after extraction to merge and repair references.                     |


### Chunk Extraction Reliability


| Problem                                                                       | How it is handled                                                                                     |
| ----------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| Provider responses may return structurally valid but truncated segment lists. | Relevance-gated retries run for high-signal chunks, with bounded attempts and best-partial retention. |
| Hard extraction failures can break progress.                                  | Retry logic catches exceptions and continues with best available chunk output when possible.          |
| Silent under-extraction is hard to detect.                                    | Post-hoc anomaly warning logs when segment count is far below run-average behavior.                   |


### Cross-Chunk Semantics


| Problem                                                                                  | How it is handled                                                                                   |
| ---------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Duplicate tasks use different wording across chunks.                                     | Resolver uses topic/context fields and merge groups to consolidate semantically equivalent actions. |
| Vague references (for example, "I'll do that") lose antecedents across chunk boundaries. | Resolver can patch descriptions and selected fields so final actions remain self-contained.         |
| Resolver overhead on tiny runs is unnecessary.                                           | Resolver is skipped when chunk/action volume is too small to justify cross-chunk reconciliation.    |


### Normalization Accuracy vs Cost


| Problem                                                                  | How it is handled                                                                                                       |
| ------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| Full-LLM normalization is expensive and unstable.                        | Rule-first pipeline (deadline parsing, verb upgrade, classification, param extraction) handles the majority of actions. |
| Some utterances contain multiple actions while others are single intent. | Rule-based compound detection gates a targeted split/no-split LLM decision only when needed.                            |
| Duplicate normalized outputs create execution noise.                     | Assignee/verb-aware similarity deduplication removes repeated tasks before execution.                                   |
| Ambiguous leftovers still occur.                                         | LLM fallback is applied narrowly for unresolved verb/classification cases.                                              |


### Execution Safety and Contact Routing


| Problem                                                 | How it is handled                                                                           |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| Raw assignee/recipient text is not directly executable. | Contact resolver maps actions to concrete emails/channels/users via relation graph context. |
| Wrong targets can cause noisy or risky side effects.    | Tool-specific enrichment strategies validate and reshape params before dispatch.            |
| Accidental production writes during development.        | Dry-run is the default; live mode must be explicitly requested.                             |


### Worker Fault Tolerance and Guardrails


| Problem                                     | How it is handled                                                                                |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Mid-run failure can waste prior LLM work.   | LangGraph Postgres checkpoints resume from the last completed node using stable thread IDs.      |
| Infinite retries on persistent errors.      | `attempt_count`/`max_attempts` guard marks tasks as permanently failed when limits are exceeded. |
| Provider rate spikes and transient outages. | Redis sliding-window limits plus exponential backoff with jitter reduce retry storms.            |


### Token Governance


| Problem                                     | How it is handled                                                                         |
| ------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Token spend can grow without visibility.    | Callback-based token accounting writes per-user/per-run/per-agent usage records.          |
| Organizations need enforceable budget caps. | Token limits (daily/monthly, global or scoped) are checked before agent execution starts. |
| Operators need a usage snapshot.            | Dashboard endpoints aggregate usage, limits, and run/task state at a glance.              |


---

## Architecture and Runtime

### API Endpoints

- `POST /runs`: create a run from uploaded transcript data.
- `GET /runs/{runId}/stream`: consume real-time SSE events (`progress`, `step_done`, `agent_done`, `run_complete`, `error`).
- `POST /runs/{runId}/actions/execute`: execute selected Slack actions from completed runs (sandboxed + rate-limited).
- JWT-protected routes support Bearer auth; SSE also supports query token for browser clients.

Worker runtime highlights:

- Celery queues: extractor, normalizer, executor.
- LangGraph Postgres checkpoints (`thread_id = {run_id}:{agent_type}`) for resume-after-failure.
- Retry guard using `agent_run_tasks.attempt_count` / `max_attempts`.
- Token tracking (`token_usage`) and hard limits (`token_limits`).
- Redis sliding-window rate limits + exponential backoff with jitter on transient provider errors.
- Redis Pub/Sub-backed SSE streaming for resilience across API restarts.
- Agent tasks chain as extractor -> normalizer -> executor and persist per-stage status in DB.

---

### Pipeline Steps Exposed in SSE

- **Extractor**: `load_transcript` -> `segmenter` -> `parallel_extractor` -> `evidence_normalizer` -> `cross_chunk_resolver` -> `global_deduplicator` -> `action_finalizer`
- **Normalizer**: `deadline_normalizer` -> `verb_enricher` -> `action_splitter` -> `deduplicator` -> `tool_classifier`
- **Executor**: `contact_resolver` -> `mcp_dispatcher`
- `parallel_extractor` emits `current`/`total` chunk progress for more accurate frontend progress tracking.

---

## Quickstart

```bash
# 1) Setup
python -m venv venv
source venv/bin/activate    # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env

# 2) Stage 1: Extract
python run_extractor.py input/input.txt

# 3) Stage 2: Normalize
python run_normalizer.py

# 4) Stage 3: Execute (dry-run)
python run_executor.py

# 5) Live execution (requires credentials)
python run_executor.py --live

# 6) API server only
python run_api.py

# 7) Combined API + worker process
python run_core.py

# 8) Start only one side from run_core
python run_core.py --api
python run_core.py --worker
```

For production-style local setup, run API and worker in separate terminals/processes.

Minimal split-process local run:

```bash
# Terminal 1
python run_api.py

# Terminal 2
python run_core.py --worker
```

---

## Requirements

- Python `3.10+`
- `pip` for dependency installation (`pip install -r requirements.txt`)
- Node.js `18+` for MCP server processes in live execution mode (`npx`-launched servers)
- Redis (required for Celery broker/result backend, Pub/Sub SSE events, and rate limiting)
- PostgreSQL (required for API persistence, run/task tables, token usage/limits, and checkpoints)

Provider requirements (choose one):

- **Gemini**: `ACTIVE_PROVIDER=gemini_mixed` and `GOOGLE_API_KEY`
- **Claude**: `ACTIVE_PROVIDER=claude` and `ANTHROPIC_API_KEY`
- **Ollama (local)**: `ACTIVE_PROVIDER=ollama` with local Ollama runtime/model available

Live execution requirements (only when not using dry-run):

- Valid integration credentials/tokens in `.env` for the tools you execute (Slack, Calendar, Notion, Jira, Gmail)
- Correct MCP server mapping in `mcp_config.json`

---

## Configuration

Use `.env` + `configs/*.env`.

```env
ACTIVE_PROVIDER=gemini_mixed   # or: claude | ollama
GOOGLE_API_KEY=...
ANTHROPIC_API_KEY=...
```

Supported provider configs:

- `gemini_mixed` -> `configs/gemini_mixed.env`
- `claude` -> `configs/claude.env`
- `ollama` -> `configs/ollama_glm.env`

Common runtime env categories:

- **Provider keys/models** (`ACTIVE_PROVIDER`, API keys, per-provider model configs)
- **Worker infra** (`REDIS_URL`, `SYNC_DATABASE_URL`, retry/rate-limit/token-limit envs)
- **Integrations** (Slack, Calendar, Notion, Jira credentials)

---

## CLI and Library Usage

CLI:

- `run_extractor.py`
- `run_normalizer.py`
- `run_executor.py`
- `run_api.py`
- `run_core.py` (combined API + worker modes)

Library calls:

- `extract_actions()` from `src/action_extractor/workflow.py`
- `normalize_actions()` from `src/action_normalizer/workflow.py`
- `execute_actions()` from `src/action_executor/workflow.py`

Typical output artifacts:

- `output/output.json` (extractor)
- `output/normalized_output.json` (normalizer)
- `output/execution_results.json` (executor)

---

## Project Structure

- `src/action_extractor/` - extractor graph and nodes
- `src/action_normalizer/` - normalizer graph and nodes
- `src/action_executor/` - executor graph and MCP dispatch
- `src/relation_graph/` - contacts + resolver
- `api/` - FastAPI routes, models, auth/integrations
- `worker/` - Celery tasks, checkpointing, token/rate limit logic
- `docs/` - stage-level technical docs
- `api/docs/` - API, worker, and integration docs
- `.cursor/plans/` - implementation plans and design rationale
- `output/` - generated JSON/log artifacts from CLI runs

---

## Safety and Execution Controls

- Dry-run executor default to prevent accidental side effects.
- MCP sandbox allowlist (`allowedTools`) + parameter validation on execution routes.
- Slack execution endpoint has per-user rate limiting.
- Token caps can block runs before LLM execution when limits are reached.
- Checkpoint resume reduces reprocessing cost after worker crashes or transient provider failures.

---

## Documentation Links

- Pipeline extractor details: `[docs/action_extractor.md](docs/action_extractor.md)`
- Pipeline normalizer details: `[docs/action_normalizer.md](docs/action_normalizer.md)`
- Pipeline executor details: `[docs/action_executor.md](docs/action_executor.md)`
- REST + SSE API contract: `[api/docs/api.md](api/docs/api.md)`
- Worker reliability/checkpointing/tokens/rate limits: `[api/docs/worker.md](api/docs/worker.md)`
- Network graph APIs: `[api/docs/network-api.md](api/docs/network-api.md)`
- Dashboard metrics: `[api/docs/dashboard.md](api/docs/dashboard.md)`
- Integrations: `[api/docs/slack.md](api/docs/slack.md)`, `[api/docs/calendar.md](api/docs/calendar.md)`, `[api/docs/notion.md](api/docs/notion.md)`, `[api/docs/jira.md](api/docs/jira.md)`, `[api/docs/auth0-implementation.md](api/docs/auth0-implementation.md)`
- Design plans and rationale: `[.cursor/plans/](.cursor/plans/)`

# ActionPipe Core (Action Item Extractor + Normalizer + Executor)

Extracts structured action items from raw meeting transcripts using a parallel LangGraph pipeline, normalizes each action into a ready-to-execute tool call, and then executes it via MCP servers — sending emails, creating Jira tickets, scheduling calendar events, posting Slack messages, and creating Notion docs. Supports multiple LLM providers — run fully locally via Ollama, or use Gemini or Claude APIs for higher accuracy.

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

### Executor

- **Relation graph** — a user-editable `contacts.json` registry mapping each person to their email, Slack handle, Notion workspace, Jira username, and named connections (teams, departments, external parties)
- **Contact resolution** — automatically enriches `tool_params` with real addresses and channels: `"to": "John"` → `"to": "john466@gmail.com"`, empty participants filled from dev team member list, garbage recipients replaced with the correct Slack channel
- **Topic-tag routing** — maps action topic tags (`"bug bash"`, `"finance"`, `"security"`) to the right connection in the graph without any manual field mapping
- **MCP dispatch** — routes each action to the appropriate MCP server: Gmail, Google Calendar, Slack, Notion, or Jira via `langchain-mcp-adapters`
- **Dry-run mode** — default behaviour logs what *would* be sent to each MCP server without spawning any processes or requiring credentials — safe for testing the full pipeline end-to-end

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

# 6. Dry-run the executor — resolve contacts + preview MCP calls (no credentials needed)
python run_executor.py                       # reads output/normalized_output.json

# 7. Live execution — actually call MCP servers (add credentials to .env first)
python run_executor.py --live                # → output/execution_results.json

# 8. Run the API server (file upload and other endpoints)
pip install -r requirements.txt             # if not already installed
python run_api.py                            # → http://localhost:8000, docs at /docs
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
| `mcp` | MCP server SDK (used by langchain-mcp-adapters) |
| `langchain-mcp-adapters` | Wraps MCP server tools as callable LangChain tools |
| `fastapi` | Web API framework |
| `uvicorn` | ASGI server for FastAPI |
| `python-multipart` | File upload support for FastAPI |

### API (FastAPI)

APIs live in the `api/` folder. To run the server:

```bash
python run_api.py
```

- **Docs:** http://localhost:8000/docs  

**Endpoints**

Full API reference: [api/docs/api.md](api/docs/api.md).

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

The normalizer reads an extractor output file (JSON array) and writes `output/normalized_output.json` with each action mapped to a specific tool.

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

**End-to-end pipeline (Stage 1 + 2) in Python:**

```python
from src.action_extractor.workflow import extract_actions
from src.action_normalizer.workflow import normalize_actions

actions    = extract_actions(transcript_raw=open("meeting.txt").read())
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

### Executor

The executor resolves contact details from the relation graph, then dispatches each action to the appropriate MCP server.

**Dry-run (default) — inspect enriched params and preview MCP calls, no credentials needed:**

```bash
python run_executor.py
```

**Custom input:**

```bash
python run_executor.py output/normalized_output.json
```

**Custom input and output:**

```bash
python run_executor.py output/normalized_output.json output/execution_results.json
```

**Use a custom contacts file:**

```bash
python run_executor.py --contacts path/to/my_contacts.json
```

**Live mode — actually call MCP servers** (add service credentials to `.env` first):

```bash
python run_executor.py --live
python run_executor.py output/normalized_output.json output/execution_results.json --live
```

**As a library:**

```python
from src.action_executor.workflow import execute_actions

# normalized is the list returned by normalize_actions()
results = execute_actions(normalized, dry_run=True)
# returns a list of result dicts: {id, tool_type, server, mcp_tool, params, status, response, error}
```

**Full end-to-end pipeline (all three stages) in Python:**

```python
from src.action_extractor.workflow import extract_actions
from src.action_normalizer.workflow import normalize_actions
from src.action_executor.workflow import execute_actions

actions    = extract_actions(transcript_raw=open("meeting.txt").read())
normalized = normalize_actions(actions, meeting_date="2026-03-05")
results    = execute_actions(normalized, dry_run=False)
```

The executor prints a summary table to stdout on completion:

```
======================================================================
  EXECUTION SUMMARY  (9 actions)
======================================================================
  [~] ab420bf4      create_notion_doc       notion/notion_create_page
           params: {"page_title": "The agreed-upon mvp definition for client delta...
  [~] d8bf7a3a      send_email              gmail/send_email
           params: {"to": "client-delta@external.com", "subject_hint": "Draft an...
  [~] 285bc753      set_calendar            calendar/create_event
           params: {"event_name": "Bug bash session...", "participants": [...], ...
----------------------------------------------------------------------
  dry_run: 9
======================================================================
```

---

## Pipelines, node details, and performance

Detailed pipeline diagrams, node-by-node descriptions, and performance notes are in separate docs:

- **[Action Extractor](docs/action_extractor.md)** — pipeline, extractor nodes (Segmenter → Action Finalizer), optimization impact, scaling
- **[Action Normalizer](docs/action_normalizer.md)** — pipeline, normalizer nodes (Deadline Normalizer → Tool Classifier), performance notes
- **[Action Executor](docs/action_executor.md)** — pipeline, contact resolver, MCP dispatcher, relation graph schema, extending contacts

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

### Executor output

Written to `output/execution_results.json` (or the path you specify). A JSON array of result objects, one per action.

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Action ID from the normalizer |
| `tool_type` | `string` | The tool that was (or would be) called |
| `server` | `string \| null` | MCP server name from `mcp_config.json` |
| `mcp_tool` | `string \| null` | Specific tool exposed by that MCP server |
| `params` | `object` | Final enriched `tool_params` after contact resolution |
| `status` | `string` | `"success"`, `"dry_run"`, `"skipped"`, or `"error"` |
| `response` | `any \| null` | Response payload from the MCP server (live mode) or a preview object (dry-run) |
| `error` | `string \| null` | Error message if `status` is `"error"`, otherwise `null` |

Example (dry-run):

```json
[
  {
    "id": "285bc753",
    "tool_type": "set_calendar",
    "server": "calendar",
    "mcp_tool": "create_event",
    "params": {
      "event_name": "Bug bash session before release for march 10th, afternoon around 2 pm.",
      "date": "2026-03-10",
      "time": "2:00 PM",
      "participants": [
        { "name": "Ash",   "email": "ash.who@gmail.com"  },
        { "name": "Kajan", "email": "kazz@gmail.com"     },
        { "name": "John",  "email": "john466@gmail.com"  }
      ]
    },
    "status": "dry_run",
    "response": { "preview": "Would invoke calendar/create_event" },
    "error": null
  },
  {
    "id": "122f5ef0",
    "tool_type": "send_notification",
    "server": "slack",
    "mcp_tool": "slack_post_message",
    "params": {
      "recipient": "#security",
      "channel": "slack",
      "message_hint": "Priya to check with the security team regarding the security review."
    },
    "status": "dry_run",
    "response": { "preview": "Would invoke slack/slack_post_message" },
    "error": null
  }
]
```

---

## Project Structure

```
agent-ai/
├── src/
│   ├── __init__.py
│   │
│   ├── action_extractor/              # ── Stage 1: Extractor ──────────────
│   │   ├── __init__.py
│   │   ├── main.py                    # CLI entry point
│   │   ├── workflow.py                # Extractor graph + extract_actions()
│   │   ├── nodes.py                   # All extractor node implementations
│   │   ├── state.py                   # Extractor graph state (TypedDict)
│   │   ├── models.py                  # Pydantic models: Segment, Action, ActionDetails
│   │   └── llm_config.py              # Per-node LLM config (loaded from .env + configs/)
│   │
│   ├── action_normalizer/             # ── Stage 2: Normalizer ─────────────
│   │   ├── __init__.py
│   │   ├── workflow.py                # Normalizer graph + normalize_actions()
│   │   ├── nodes.py                   # All normalizer node implementations
│   │   ├── state.py                   # Normalizer graph state (TypedDict)
│   │   ├── models.py                  # Pydantic models: NormalizedAction, ToolType
│   │   └── data.py                    # Rule-based data: verb dict, tool map, patterns
│   │
│   ├── relation_graph/                # ── Contact registry ────────────────
│   │   ├── __init__.py
│   │   ├── contacts.json              # People, their channels, and connections
│   │   ├── models.py                  # Pydantic models: Person, Connection, Member
│   │   └── resolver.py                # ContactResolver — enriches tool_params
│   │
│   └── action_executor/               # ── Stage 3: Executor ───────────────
│       ├── __init__.py
│       ├── workflow.py                # Executor graph + execute_actions()
│       ├── nodes.py                   # contact_resolver_node, mcp_dispatcher_node
│       ├── state.py                   # Executor graph state (TypedDict)
│       └── mcp_clients.py             # MCPDispatcher (dry-run + live via langchain-mcp-adapters)
│
├── input/
│   ├── input.txt                      # Default input transcript
│   ├── input_very_small.txt           # Small test transcript (63 turns)
│   ├── input_small.txt                # Medium test transcript
│   ├── input_large.txt                # Large test transcript
│   └── real_input.txt                 # Real meeting transcript
├── output/                            # Generated on run (gitignored)
│   ├── output.json                    # Stage 1 output
│   ├── normalized_output.json         # Stage 2 output
│   ├── execution_results.json         # Stage 3 output
│   ├── output_log.txt                 # Extractor execution log
│   └── normalizer_log.txt             # Normalizer execution log
├── configs/
│   ├── gemini_mixed.env               # Gemini Flash provider config
│   ├── claude.env                     # Claude Haiku provider config
│   └── ollama_glm.env                 # Ollama local provider config
├── tests/
│   └── test_langchain_to_llm.py
├── docs/
│   ├── action_extractor.md            # Extractor pipeline, nodes, performance
│   ├── action_normalizer.md           # Normalizer pipeline, nodes, performance
│   └── action_executor.md             # Executor pipeline, relation graph, MCP dispatch
├── mcp_config.json                    # MCP server definitions (ToolType → server)
├── run_extractor.py                   # Stage 1 CLI runner
├── run_normalizer.py                  # Stage 2 CLI runner (with summary table)
├── run_executor.py                    # Stage 3 CLI runner (dry-run + --live)
├── .env                               # API keys, ACTIVE_PROVIDER, MCP credentials (gitignored)
└── requirements.txt
```

---

## Requirements

- Python 3.10+
- Node.js 18+ (required only in live mode — MCP servers are launched as `npx` processes)
- **`gemini_mixed` provider:** `GOOGLE_API_KEY` and internet access
- **`claude` provider:** `ANTHROPIC_API_KEY` and internet access
- **`ollama` provider:** Ollama running locally with `glm-4.7-flash` pulled (or any OpenAI-compatible model at `http://localhost:11434/v1`)
- **Executor live mode:** credentials for each MCP service you want to use (see `.env.example` for the full list — `SLACK_BOT_TOKEN`, `NOTION_API_TOKEN`, `JIRA_*`, OAuth paths for Gmail/Calendar)
