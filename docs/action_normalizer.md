# Action Normalizer

Pipeline and node details for the action normalizer (LangGraph workflow that normalizes extractor output into tool-ready actions).

---

## Pipeline

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

### 1. Deadline Normalizer *(no LLM)*

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

### 2. Verb Enricher *(no LLM)*

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

### 3. Action Splitter *(LLM for compound candidates only)*

Detects and splits descriptions that contain two or more independently executable actions.

**Rule-based detection** — flags a description as a compound candidate when it contains:
- A conjunction keyword (`and`, `as well as`, `additionally`)
- Two or more distinct action verbs from the known verb set

**LLM split decision** — only compound candidates are sent to the LLM with a tight prompt that includes canonical examples:

- `"Investigate flaky tests and fix them"` → `["Investigate flaky tests", "Fix flaky tests"]` ✓ split
- `"Create and track a task for fixing alerts"` → single Jira ticket ✗ no split

Each split action inherits the parent's assignee, deadline, confidence, and `source_spans`, and carries a `parent_id` linking back to the original compound action.

---

### 4. Deduplicator *(no LLM)*

Removes actions that describe the same real-world task. Two actions are considered duplicates when **all** of:

- Same `assignee` (or at least one is null)
- Same `verb`
- Description Jaccard similarity ≥ 0.6 (after removing stop words)

The representative is the highest-confidence action; `source_spans` are merged from all duplicates.

---

### 5. Tool Classifier *(no LLM)*

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

## Performance

The normalizer is designed for low latency: most steps are rule-based (deadline parsing, verb upgrade, tool classification). LLM is used only for compound-action splitting (when a description contains multiple verbs) and for the rare case when tool classification cannot be determined from verb, category, and keywords. Typical runtime is on the order of seconds for tens of actions.
