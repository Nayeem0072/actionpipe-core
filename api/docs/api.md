# API Documentation

REST API for pipeline runs: create a run (upload meeting transcript + metadata), then subscribe to a Server-Sent Events (SSE) stream for real-time progress. The pipeline runs **extractor** → **normalizer** → **executor**.

**Base URL (local):** `http://localhost:8000`  
**Interactive docs:** `http://localhost:8000/docs`

Start the server from the project root:

```bash
python run_api.py
```

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/runs` | Create a new pipeline run. **Protected:** requires JWT. Upload a file (or pass by reference), start processing, get `runId` and `streamUrl`. |
| `GET`  | `/runs/{runId}/stream` | SSE stream for real-time progress. **Protected:** requires JWT (header or `?token=`). |
| `POST` | `/runs/{runId}/actions/execute` | Execute selected Slack actions from a completed run. **Protected:** requires JWT. Sandboxed (allowlist + validation); rate limited per user. |

All protected routes accept the JWT via **`Authorization: Bearer <token>`** or, for SSE where headers may be limited, via the **`token`** query parameter (e.g. `/runs/{runId}/stream?token=<jwt>`). The token is validated (Auth0 JWT); the token's claims and the DB user (get-or-create) are available as request "user details".

---

## POST /runs

Create a pipeline run. Processing starts asynchronously; use the returned `streamUrl` to consume progress via SSE.

### Request

**Authentication:** required. Send JWT via `Authorization: Bearer <token>`.

**Content-Type:** either `multipart/form-data` or `application/json` (for upload by reference).


| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | file | Yes | Meeting transcript. Allowed: `.txt`, `.csv`, `.pdf`, `.doc`. Max size: **15 MB**. |
| `meetingDate` | string | No | Date of the meeting, e.g. `YYYY-MM-DD`. |
| `language` | string | No | Language code, e.g. `en`, `bn`. |

**Example (curl):**

```bash
curl -X POST http://localhost:8000/runs \
  -H "Authorization: Bearer <your-jwt>" \
  -F "file=@/path/to/transcript.txt" \
  -F "meetingDate=2026-03-07" \
  -F "language=en"
```


### Response

**Status:** `201 Created`

**Body (JSON):**

| Field | Type | Description |
|-------|------|-------------|
| `runId` | string | Unique id for this run. Use it in the stream URL. |
| `streamUrl` | string | Path to the SSE stream, e.g. `GET /runs/{runId}/stream`. |

**Example:**

```json
{
  "runId": "a1b2c3d4e5f6",
  "streamUrl": "/runs/a1b2c3d4e5f6/stream"
}
```

### Errors

| Status | Condition |
|--------|-----------|
| `400` | Missing `file` (multipart) or `fileRef` (JSON); or file type not allowed (allowed: `.txt`, `.csv`, `.pdf`, `.doc`). |
| `401` | Missing or invalid JWT (use `Authorization: Bearer <token>`). |
| `404` | JSON body: `fileRef` points to a path that does not exist. |
| `413` | File larger than 15 MB. |
| `503` | Auth0 not configured or JWKS unavailable. |

---

## GET /runs/{runId}/stream

Real-time progress for the run. Streams Server-Sent Events until the pipeline finishes or errors.

### Request

| Item | Value |
|------|--------|
| **Path** | `runId` — from `POST /runs` response. |
| **Auth** | JWT via `Authorization: Bearer <token>` or query param `?token=<jwt>` (recommended for SSE). |
| **Headers** | `Accept: text/event-stream` (recommended). |

**Example (curl):**

```bash
# With Bearer header
curl -N -H "Accept: text/event-stream" -H "Authorization: Bearer <your-jwt>" \
  http://localhost:8000/runs/a1b2c3d4e5f6/stream

# With token query param (e.g. for EventSource)
curl -N -H "Accept: text/event-stream" \
  "http://localhost:8000/runs/a1b2c3d4e5f6/stream?token=<your-jwt>"
```

### Response

**Status:** `200 OK`

**Headers:**

| Header | Value |
|--------|--------|
| `Content-Type` | `text/event-stream` |
| `Cache-Control` | `no-cache` |
| `Connection` | `keep-alive` |

**Body:** SSE stream. Each message has an optional `event` type and a `data` line (JSON).

### SSE event types

| Event | Description | Data payload |
|-------|-------------|--------------|
| `progress` | An agent is working on a step. | `agent`, `step`, `status`; optional `current`, `total` (e.g. Parallel Executor 8/11). |
| `step_done` | One step of an agent finished. | `agent`, `step`. |
| `agent_done` | Entire agent finished. | `agent` (`"extractor"` \| `"normalizer"` \| `"executor"`). |
| `run_complete` | Whole pipeline finished. | Optional `summary` (e.g. `actions_extracted`, `actions_normalized`, `actions_executed`); `executor_actions`: list of executor result objects (id, tool_type, server, mcp_tool, params, status, response, error). |
| `error` | Run or step failed. | `message`; optional `code`, `agent`, `step`. |

### Extractor steps (SSE `step` values)

Progress follows the extractor graph nodes so the frontend can show accurate steps:

| Step | Description |
|------|-------------|
| `load_transcript` | Load transcript from the uploaded/referenced file. |
| `segmenter` | Split transcript into chunks (by speaker turns, 20 turns per chunk). |
| `parallel_extractor` | Extract segments from each chunk (LLM). Progress events with `current`/`total` (e.g. 7/12) as each chunk completes. |
| `evidence_normalizer` | Clean ASR noise, drop meta-actions, convert segments to actions. |
| `cross_chunk_resolver` | Merge cross-chunk duplicates, resolve vague references. |
| `global_deduplicator` | Remove duplicate actions by similarity. |
| `action_finalizer` | Schema enforcement, sort, drop low-confidence. |

### Normalizer steps (SSE `step` values)

After the extractor, the **normalizer** runs. Steps follow the normalizer graph nodes:

| Step | Description |
|------|-------------|
| `deadline_normalizer` | Convert free-text deadlines to ISO dates; convert extractor actions to NormalizedAction. |
| `verb_enricher` | Extract/upgrade verbs (rule-based + rare LLM fallback). |
| `action_splitter` | Detect and split compound actions (LLM for candidates). |
| `deduplicator` | Remove duplicates by Jaccard similarity (same assignee, verb). |
| `tool_classifier` | Classify into ToolType + extract tool params (rule-based + rare LLM). |

### Executor steps (SSE `step` values)

After the normalizer, the **executor** runs. Steps follow the executor graph nodes:

| Step | Description |
|------|-------------|
| `contact_resolver` | For each normalized action, resolve real contacts from the relation graph (LLM) and enrich `tool_params`. |
| `mcp_dispatcher` | Dispatch each enriched action to the correct MCP server tool (Gmail, Calendar, Slack, Notion, Jira). Default is dry-run (no live calls). |

### executor_actions (run_complete payload)

When the pipeline finishes, the `run_complete` event includes an `executor_actions` array: one object per action that was sent to the executor. Each object has the following shape:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Action id (e.g. from extractor/normalizer). |
| `tool_type` | string | Tool type, e.g. `send_email`, `create_calendar_event`. |
| `server` | string | MCP server name, e.g. `gmail`, `google_calendar`. |
| `mcp_tool` | string | MCP tool name invoked (often same as `tool_type`). |
| `params` | object | Resolved tool parameters (e.g. `to`, `subject_hint`, `body_hint` for email). |
| `status` | string | `"success"` \| `"dry_run"` \| `"skipped"` \| `"error"`. |
| `response` | object \| null | Tool response; in dry-run, often `{ "preview": "Would invoke …" }`. |
| `error` | string \| null | Error message when `status` is `"error"`. |

**Example `executor_actions`:**

```json
[
  {
    "id": "ae69d78c",
    "tool_type": "send_email",
    "server": "gmail",
    "mcp_tool": "send_email",
    "params": {
      "to": "client-delta@external.com",
      "subject_hint": "Draft an update email to the client to reset expectations, i",
      "body_hint": "Draft an update email to the client to reset expectations, including the phased delivery plan and mentioning the scope change impact."
    },
    "status": "dry_run",
    "response": {
      "preview": "Would invoke gmail/send_email"
    },
    "error": null
  }
]
```

See [Action Executor](action_executor.md) for pipeline and step details.

### Example stream (extractor + normalizer + executor)

```
event: progress
data: {"agent": "extractor", "step": "load_transcript", "status": "running"}

event: step_done
data: {"agent": "extractor", "step": "load_transcript"}

event: step_done
data: {"agent": "extractor", "step": "segmenter"}

event: progress
data: {"agent": "extractor", "step": "parallel_extractor", "status": "running", "current": 1, "total": 12}

event: progress
data: {"agent": "extractor", "step": "parallel_extractor", "status": "running", "current": 2, "total": 12}

… (further parallel_extractor progress: 3/12, 4/12, …)

event: step_done
data: {"agent": "extractor", "step": "parallel_extractor"}

event: step_done
data: {"agent": "extractor", "step": "evidence_normalizer"}

event: step_done
data: {"agent": "extractor", "step": "cross_chunk_resolver"}

event: step_done
data: {"agent": "extractor", "step": "global_deduplicator"}

event: step_done
data: {"agent": "extractor", "step": "action_finalizer"}

event: agent_done
data: {"agent": "extractor"}

event: progress
data: {"agent": "normalizer", "step": "deadline_normalizer", "status": "running"}

event: step_done
data: {"agent": "normalizer", "step": "deadline_normalizer"}

event: progress
data: {"agent": "normalizer", "step": "verb_enricher", "status": "running"}

event: step_done
data: {"agent": "normalizer", "step": "verb_enricher"}

event: step_done
data: {"agent": "normalizer", "step": "action_splitter"}

event: step_done
data: {"agent": "normalizer", "step": "deduplicator"}

event: step_done
data: {"agent": "normalizer", "step": "tool_classifier"}

event: agent_done
data: {"agent": "normalizer"}

event: progress
data: {"agent": "executor", "step": "contact_resolver", "status": "running"}

event: step_done
data: {"agent": "executor", "step": "contact_resolver"}

event: progress
data: {"agent": "executor", "step": "mcp_dispatcher", "status": "running"}

event: step_done
data: {"agent": "executor", "step": "mcp_dispatcher"}

event: agent_done
data: {"agent": "executor"}

event: run_complete
data: {"summary": {"actions_extracted": 5, "actions_normalized": 4, "actions_executed": 4}, "executor_actions": [{"id": "...", "tool_type": "send_email", "status": "success", ...}]}
```

### Errors

| Status | Condition |
|--------|-----------|
| `401` | Missing or invalid JWT (use header or `?token=`). |
| `404` | `runId` not found (invalid or run never created). |
| `503` | Auth0 not configured or JWKS unavailable. |

---

## POST /runs/{runId}/actions/execute

Execute selected **Slack** actions from a completed run. The run must have finished (a `run_complete` event with `executor_actions`). Only actions with `server: "slack"` (tool type `send_notification`) can be executed; others return `400`. The Slack MCP server uses the **user's token from the `user_tokens` table** (from `/slack/connect`). Slack must be connected for the current user or the request returns `403`. Sandboxing (allowlist and parameter validation) and per-user rate limiting apply.

### Request

**Authentication:** required. Send JWT via `Authorization: Bearer <token>`.

**Content-Type:** `application/json`

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `actionIds` | array of string | Yes | Action `id` values from the run’s `executor_actions` (Slack actions only). Min length 1. |

**Example (curl):**

```bash
curl -X POST http://localhost:8000/runs/a1b2c3d4e5f6/actions/execute \
  -H "Authorization: Bearer <your-jwt>" \
  -H "Content-Type: application/json" \
  -d '{"actionIds": ["388e66b7", "9b82695e"]}'
```

### Response

**Status:** `200 OK`

**Body (JSON):**

| Field | Type | Description |
|-------|------|-------------|
| `executor_actions` | array | One object per executed action: `id`, `tool_type`, `server`, `mcp_tool`, `params`, `status` (`"success"` \| `"error"`), `response`, `error`. Same shape as in the stream’s `run_complete` payload. |

### Sandbox and rate limits

- **Tool allowlist:** Only tools listed in `mcp_config.json` → `allowedTools` are invoked. For Slack, only `slack_post_message` is allowed.
- **Parameter validation:** Slack params are mapped to `channel_id` and `text`; message length is capped; content that matches instruction-override patterns is rejected.
- **Rate limit:** Per user, max `SLACK_EXECUTE_LIMIT_PER_MINUTE` Slack executions per 60-second window (default 10). Returns `429` when exceeded.

### Errors

| Status | Condition |
|--------|-----------|
| `400` | Unknown `actionIds`; or one or more ids are not Slack actions (only Slack actions can be executed). |
| `401` | Missing or invalid JWT. |
| `403` | Slack not connected for the current user; connect via `/slack/connect` first. |
| `404` | Run not found or access denied; or run has no completed response yet. |
| `429` | Rate limit exceeded (too many Slack executions in the last minute). |
| `503` | Auth0 not configured or JWKS unavailable. |

---

## Pipeline (current behavior)

The pipeline runs **extractor** → **normalizer** → **executor**. Progress is emitted at node level for all three.

- **Extractor:** load_transcript → segmenter → parallel_extractor (with current/total) → evidence_normalizer → cross_chunk_resolver → global_deduplicator → action_finalizer.
- **Normalizer:** deadline_normalizer → verb_enricher → action_splitter → deduplicator → tool_classifier.
- **Executor:** contact_resolver (LLM + relation graph) → mcp_dispatcher (MCP tool calls or dry-run). See [Action Executor](action_executor.md).
