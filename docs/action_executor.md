# Action Executor

Pipeline and node details for the action executor (Stage 3 — resolves real contact details from the relation graph and dispatches each action to the appropriate MCP server tool).

---

## Steps (for Run / SSE API)

The executor runs **two steps** in order. The Run and SSE APIs emit `progress` and `step_done` events for each.

| Step | Description |
|------|-------------|
| **contact_resolver** | For each normalized action, the LLM picks the right connection from the relation graph and enriches `tool_params` (e.g. `to`, `participants`, `recipient`, `assignee`, `workspace`). |
| **mcp_dispatcher** | Each enriched action is dispatched to the correct MCP server tool (Gmail, Calendar, Slack, Notion, Jira). In dry-run mode, no live calls are made; results are marked `dry_run`. |

SSE events for the executor use `agent: "executor"` and `step: "contact_resolver"` or `step: "mcp_dispatcher"`.

---

## Pipeline

A two-node LangGraph graph. It consumes the normalizer's output (`normalized_output.json`) and produces an execution result for every action.

```
┌──────────────────────────┐
│   Contact Resolver       │  LLM picks the right connection; enriches tool_params  (LLM)
│                          │  from the relation graph (src/relation_graph/contacts.json)
└────────────┬─────────────┘
             │
┌────────────▼─────────────┐
│   MCP Dispatcher         │  Routes each action to the correct MCP server tool     (no LLM)
│                          │
│  send_email        → Gmail MCP             │
│  set_calendar      → Google Calendar MCP  │
│  send_notification → Slack MCP            │
│  create_notion_doc → Notion MCP           │
│  create_jira_task  → Jira MCP             │
└────────────┬─────────────┘
             │
            END
```

---

## Relation Graph

The relation graph (`src/relation_graph/contacts.json`) is the registry used by the Contact Resolver. It stores every meeting participant's contact details and their named connections — external parties, departments, and groups. The LLM reads these connections at runtime to decide which one is relevant for each action.

### Schema

```
people
└── <PersonName>
    ├── email             (person's own address)
    ├── slack_handle      (e.g. "@john")
    ├── notion_workspace
    ├── jira_user
    └── connections
        └── <connection_key>
            ├── email         (for email/non-Slack channels)
            ├── slack_channel (e.g. "#finance")
            └── members[]     (for groups — name + email + slack_handle per member)
```

There is no keyword hint table. The LLM reasons from the action description, tool type, and topic tags directly against the list of available connections.

### Example entries

```json
{
  "people": {
    "Priya": {
      "email": "priya@company.com",
      "slack_handle": "@priya",
      "notion_workspace": "company-workspace",
      "jira_user": "priya",
      "connections": {
        "security_team": {
          "slack_channel": "#security",
          "email": "security@company.com"
        }
      }
    },
    "John": {
      "email": "john466@gmail.com",
      "slack_handle": "@john",
      "jira_user": "john",
      "connections": {
        "finance": {
          "email": "finance.companyname@gmail.com",
          "slack_channel": "#finance"
        },
        "dev_team": {
          "slack_channel": "#dev-team",
          "members": [
            { "name": "Ash",   "email": "ash.who@gmail.com"  },
            { "name": "Kajan", "email": "kazz@gmail.com"     }
          ]
        },
        "client_delta": {
          "email": "client-delta@external.com"
        }
      }
    }
  }
}
```

---

## Node Details

### 1. Contact Resolver *(LLM)*

Iterates every `NormalizedAction` and enriches its `tool_params` dict using the relation graph. The original action is not mutated — a deep copy is returned.

#### LLM connection resolution

For each action, the resolver calls the LLM with:

- The full action description
- The assignee's name
- The tool type (`send_email`, `set_calendar`, etc.)
- The action's topic tags
- A human-readable summary of every connection the assignee has in `contacts.json`

The LLM returns a structured `ConnectionResolution` object:

```python
class ConnectionResolution(BaseModel):
    connection_key: Optional[str]  # which connection to use, or null
    confidence: float              # 0.0 (no idea) → 1.0 (certain)
    reasoning: str                 # one-sentence explanation
```

This result is stored on the enriched action under `connection_resolution` and travels through to the final executor output alongside the MCP dispatch result.

The LLM uses `LOCAL_EXTRACTOR_CONFIG` (the same provider and model configured for the rest of the pipeline via `.env` and `configs/`).

**Prompt given to the LLM (condensed):**

```
Action description: Schedule a bug bash session before release for March 10th, afternoon around 2 PM.
Assignee: John
Action type: set_calendar
Topic tags: bug bash, scheduling, release

John's available connections:
  [finance]      slack #finance, email finance.companyname@gmail.com
  [dev_team]     group — members: Ash (ash.who@gmail.com), Kajan (kazz@gmail.com)
  [client_delta] email client-delta@external.com
```

**LLM response:**

```json
{
  "connection_key": "dev_team",
  "confidence": 0.90,
  "reasoning": "The action is to schedule a bug bash session, which is a development-related activity, making the dev_team the most relevant connection."
}
```

#### Resolution strategy per tool type

Once the LLM has chosen a connection, the resolver applies it per tool type:

| Tool | What gets resolved | How |
|---|---|---|
| `send_email` | `to` → real email | LLM-chosen connection email, or person's own email as fallback |
| `set_calendar` | `participants` → `[{name, email}]` | LLM-chosen connection members; organiser appended; `time` auto-parsed from event name |
| `send_notification` | `recipient` → Slack channel or email | LLM-chosen connection channel/email; garbage values (e.g. `"the"`) treated as empty |
| `create_jira_task` | `assignee` → Jira username | `jira_user` field from the person's entry (no LLM needed) |
| `create_notion_doc` | `workspace` → Notion workspace ID | `notion_workspace` field from the person's entry (no LLM needed) |

#### Enrichment examples (with LLM decisions)

```
Action d8bf7a3a  send_email  (John → client email)
  LLM decision : connection_key=client_delta  confidence=0.95
  Reasoning    : "The action is to draft an update email to the client, and
                  'client_delta' is the only external client email connection."
  Before:  tool_params.to = "John"
  After:   tool_params.to = "client-delta@external.com"

Action 285bc753  set_calendar  (bug bash)
  LLM decision : connection_key=dev_team  confidence=0.90
  Reasoning    : "The action is to schedule a bug bash session, which is a
                  development-related activity, making dev_team the most relevant."
  Before:  tool_params.participants = []
           tool_params.time = null
  After:   tool_params.participants = [
               {"name": "Ash",   "email": "ash.who@gmail.com"},
               {"name": "Kajan", "email": "kazz@gmail.com"},
               {"name": "John",  "email": "john466@gmail.com"}
           ]
           tool_params.time = "2:00 PM"   ← regex from event_name

Action 966e958a  send_notification  (John → finance)
  LLM decision : connection_key=finance  confidence=1.00
  Reasoning    : "The action is a notification explicitly directed towards the
                  'finance department', and a connection for finance is available."
  Before:  tool_params.recipient = "the"   ← garbage extraction
  After:   tool_params.recipient = "#finance"
           tool_params.channel   = "slack"

Action 122f5ef0  send_notification  (Priya → security team)
  LLM decision : connection_key=security_team  confidence=1.00
  Reasoning    : "The action explicitly mentions checking with the 'security team',
                  which directly matches an available connection."
  Before:  tool_params.recipient = null
  After:   tool_params.recipient = "#security"
```

#### LLM fallback behaviour

If the LLM call fails (network error, malformed response), the resolver logs a warning, sets `connection_key=null`, `confidence=0.0`, and continues — the action is still enriched with whatever static data is available (Jira user, Notion workspace, the person's own email, etc.).

---

### 2. MCP Dispatcher *(no LLM)*

Reads `mcp_config.json` and routes each enriched action to its MCP server using `langchain-mcp-adapters`.

**Tool type → MCP server mapping:**

| ToolType | MCP Server | MCP Tool |
|---|---|---|
| `send_email` | `@googleapis/mcp-server-gmail` | `send_email` |
| `set_calendar` | `@googleapis/mcp-server-calendar` | `create_event` |
| `send_notification` | `@modelcontextprotocol/server-slack` | `slack_post_message` |
| `create_notion_doc` | `@notionhq/notion-mcp-server` | `notion_create_page` |
| `create_jira_task` | `jira-mcp-server` | `jira_create_issue` |
| `general_task` | *(none — skipped)* | — |

**Dispatch flow (live mode):**

1. Load `mcp_config.json`, resolve `${ENV_VAR}` placeholders from the process environment.
2. Launch the target MCP server process via `MultiServerMCPClient` (stdio transport).
3. Discover available tools from the server.
4. Call the mapped tool with the enriched `tool_params`.
5. Capture the response; record `status: "success"` or `status: "error"` with the full error message.

**Dry-run mode (default):** no processes are launched. Each call logs a preview of what *would* be sent and records `status: "dry_run"`. This is the default when running `run_executor.py` without `--live`.

**Result schema per action:**

```json
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
      {"name": "Ash",   "email": "ash.who@gmail.com"},
      {"name": "Kajan", "email": "kazz@gmail.com"},
      {"name": "John",  "email": "john466@gmail.com"}
    ]
  },
  "status": "dry_run",
  "response": { "preview": "Would invoke calendar/create_event" },
  "error": null
}
```

The `connection_resolution` field also appears on each enriched action (before dispatch), carrying the LLM's decision:

```json
"connection_resolution": {
  "connection_key": "dev_team",
  "confidence": 0.90,
  "reasoning": "The action is to schedule a bug bash session, which is a development-related activity."
}
```

---

## MCP Server Configuration

All server definitions live in `mcp_config.json` at the project root. Each entry maps a logical name to the npm package that implements the MCP server, plus the environment variables it requires.

```json
{
  "mcpServers": {
    "gmail":    { "command": "npx", "args": ["-y", "@googleapis/mcp-server-gmail"],
                  "env": { "GMAIL_OAUTH_PATH": "${GMAIL_OAUTH_PATH}" } },
    "calendar": { "command": "npx", "args": ["-y", "@googleapis/mcp-server-calendar"],
                  "env": { "CALENDAR_OAUTH_PATH": "${CALENDAR_OAUTH_PATH}" } },
    "slack":    { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-slack"],
                  "env": { "SLACK_BOT_TOKEN": "${SLACK_BOT_TOKEN}", "SLACK_TEAM_ID": "${SLACK_TEAM_ID}" } },
    "notion":   { "command": "npx", "args": ["-y", "@notionhq/notion-mcp-server"],
                  "env": { "NOTION_API_TOKEN": "${NOTION_API_TOKEN}" } },
    "jira":     { "command": "npx", "args": ["-y", "jira-mcp-server"],
                  "env": { "JIRA_URL": "${JIRA_URL}", "JIRA_EMAIL": "${JIRA_EMAIL}",
                           "JIRA_API_TOKEN": "${JIRA_API_TOKEN}", "JIRA_PROJECT_KEY": "${JIRA_PROJECT_KEY}" } }
  },
  "toolTypeToServer": {
    "send_email":        "gmail",
    "set_calendar":      "calendar",
    "send_notification": "slack",
    "create_notion_doc": "notion",
    "create_jira_task":  "jira",
    "general_task":      null
  }
}
```

To add a new integration: add an entry under `mcpServers`, add the mapping under `toolTypeToServer`, and add the required env vars to `.env`.

---

## Extending the Relation Graph

No code changes are needed — the resolver reads `contacts.json` at startup and the LLM reasons from whatever connections are present.

**Add a new person:**

```json
"Alex": {
  "email": "alex@company.com",
  "slack_handle": "@alex",
  "jira_user": "alex",
  "connections": {}
}
```

**Add a connection to an existing person:**

```json
"connections": {
  "qa_team": {
    "slack_channel": "#qa",
    "members": [
      { "name": "Sam", "email": "sam@company.com" }
    ]
  }
}
```

Once the connection is in `contacts.json`, the LLM will consider it automatically when resolving future actions — no keyword mappings or hint entries are required.

---

## Performance

Contact resolution makes one LLM call per action (using the project's configured provider via `LOCAL_EXTRACTOR_CONFIG`). For a typical post-meeting batch of 5–15 actions, this adds roughly 3–15 seconds of wall time on top of the normalizer. LLM calls per action are independent and could be parallelised in a future optimisation.

MCP dispatch latency is dominated by the target service (Gmail, Slack, etc.) and network round-trip — typically 500 ms–2 s per call. In dry-run mode the dispatch phase completes in well under 1 second for any number of actions.
