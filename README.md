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

| Problem | How it is handled |
| --- | --- |
| Sequential chunk extraction makes runtime scale poorly. | Relevant chunks are extracted concurrently; wall time is driven by the slowest chunk, not total chunk count. |
| Too many small chunks increase LLM calls. | Transcript is chunked into 20-turn windows to reduce overhead while preserving local context. |
| LLM relevance checks add latency and cost. | A free keyword relevance scorer filters low-signal chunks before extraction. |
| Cross-chunk context can cause many extra calls. | A single cross-chunk resolver pass runs after extraction to merge and repair references. |

### Chunk Extraction Reliability

| Problem | How it is handled |
| --- | --- |
| Provider responses may return structurally valid but truncated segment lists. | Relevance-gated retries run for high-signal chunks, with bounded attempts and best-partial retention. |
| Hard extraction failures can break progress. | Retry logic catches exceptions and continues with best available chunk output when possible. |
| Silent under-extraction is hard to detect. | Post-hoc anomaly warning logs when segment count is far below run-average behavior. |

### Cross-Chunk Semantics

| Problem | How it is handled |
| --- | --- |
| Duplicate tasks use different wording across chunks. | Resolver uses topic/context fields and merge groups to consolidate semantically equivalent actions. |
| Vague references (for example, "I'll do that") lose antecedents across chunk boundaries. | Resolver can patch descriptions and selected fields so final actions remain self-contained. |
| Resolver overhead on tiny runs is unnecessary. | Resolver is skipped when chunk/action volume is too small to justify cross-chunk reconciliation. |

### Normalization Accuracy vs Cost

| Problem | How it is handled |
| --- | --- |
| Full-LLM normalization is expensive and unstable. | Rule-first pipeline (deadline parsing, verb upgrade, classification, param extraction) handles the majority of actions. |
| Some utterances contain multiple actions while others are single intent. | Rule-based compound detection gates a targeted split/no-split LLM decision only when needed. |
| Duplicate normalized outputs create execution noise. | Assignee/verb-aware similarity deduplication removes repeated tasks before execution. |
| Ambiguous leftovers still occur. | LLM fallback is applied narrowly for unresolved verb/classification cases. |

### Execution Safety and Contact Routing

| Problem | How it is handled |
| --- | --- |
| Raw assignee/recipient text is not directly executable. | Contact resolver maps actions to concrete emails/channels/users via relation graph context. |
| Wrong targets can cause noisy or risky side effects. | Tool-specific enrichment strategies validate and reshape params before dispatch. |
| Accidental production writes during development. | Dry-run is the default; live mode must be explicitly requested. |

### Worker Fault Tolerance and Guardrails

| Problem | How it is handled |
| --- | --- |
| Mid-run failure can waste prior LLM work. | LangGraph Postgres checkpoints resume from the last completed node using stable thread IDs. |
| Infinite retries on persistent errors. | `attempt_count`/`max_attempts` guard marks tasks as permanently failed when limits are exceeded. |
| Provider rate spikes and transient outages. | Redis sliding-window limits plus exponential backoff with jitter reduce retry storms. |

### Token Governance

| Problem | How it is handled |
| --- | --- |
| Token spend can grow without visibility. | Callback-based token accounting writes per-user/per-run/per-agent usage records. |
| Organizations need enforceable budget caps. | Token limits (daily/monthly, global or scoped) are checked before agent execution starts. |
| Operators need a usage snapshot. | Dashboard endpoints aggregate usage, limits, and run/task state at a glance. |

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

- Pipeline extractor details: [docs/action_extractor.md](docs/action_extractor.md)
- Pipeline normalizer details: [docs/action_normalizer.md](docs/action_normalizer.md)
- Pipeline executor details: [docs/action_executor.md](docs/action_executor.md)
- REST + SSE API contract: [api/docs/api.md](api/docs/api.md)
- Worker reliability/checkpointing/tokens/rate limits: [api/docs/worker.md](api/docs/worker.md)
- Network graph APIs: [api/docs/network-api.md](api/docs/network-api.md)
- Dashboard metrics: [api/docs/dashboard.md](api/docs/dashboard.md)
- Integrations: [api/docs/slack.md](api/docs/slack.md), [api/docs/calendar.md](api/docs/calendar.md), [api/docs/notion.md](api/docs/notion.md), [api/docs/jira.md](api/docs/jira.md), [api/docs/auth0-implementation.md](api/docs/auth0-implementation.md)
- Design plans and rationale: [.cursor/plans/](.cursor/plans/)

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

- Pipeline extractor details: [docs/action_extractor.md](docs/action_extractor.md)
- Pipeline normalizer details: [docs/action_normalizer.md](docs/action_normalizer.md)
- Pipeline executor details: [docs/action_executor.md](docs/action_executor.md)
- REST + SSE API contract: [api/docs/api.md](api/docs/api.md)
- Worker reliability/checkpointing/tokens/rate limits: [api/docs/worker.md](api/docs/worker.md)
- Network graph APIs: [api/docs/network-api.md](api/docs/network-api.md)
- Dashboard metrics: [api/docs/dashboard.md](api/docs/dashboard.md)
- Integrations: [api/docs/slack.md](api/docs/slack.md), [api/docs/calendar.md](api/docs/calendar.md), [api/docs/notion.md](api/docs/notion.md), [api/docs/jira.md](api/docs/jira.md), [api/docs/auth0-implementation.md](api/docs/auth0-implementation.md)
- Design plans and rationale: [.cursor/plans/](.cursor/plans/)

