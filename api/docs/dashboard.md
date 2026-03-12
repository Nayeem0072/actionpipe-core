# Dashboard API

This project now exposes a per-user dashboard summary endpoint:

```http
GET /dashboard/summary
Authorization: Bearer <access-token>
```

The endpoint is authenticated and always returns data for the currently logged-in user.

## Response shape

The response includes:

- `tokens`
  - `used_total`
  - `prompt_total`
  - `completion_total`
  - `used_this_month`
  - `allocated_this_month`
  - `remaining_this_month`
  - `is_unlimited`
  - `by_agent`
- `runs`
  - `requested`
  - `success`
  - `completed`
  - `failed`
  - `in_progress`
- `agentStages`
  - extractor, normalizer, executor counts grouped by status
- `actions`
  - `extracted`
  - `normalized`
  - `executed`
- `integrationsFound`
  - counts by provider/tool target such as `slack`, `notion`, `jira`, `gmail`, `calendar`, `general_task`
- `integrationsConnected`
  - connected account counts from `UserToken`

## Metric sources

- `TokenUsage` powers token totals and per-agent usage.
- `TokenLimit` powers the monthly allocation and remaining quota.
- `RunRequestLog` powers requested run counts.
- `AgentRunTask` powers stage status counts and completed/failed/in-progress run counts.
- `RunResponseLog` powers action totals and found integration counts from completed runs.
- `UserToken` powers connected integration counts.

## Quota behavior

New users receive a default monthly quota of `100000` tokens.

This is created automatically when a user is first created from Auth0. The quota is stored as:

- `TokenLimit.user_id = <user id>`
- `TokenLimit.agent_type = NULL`
- `TokenLimit.period = "monthly"`
- `TokenLimit.max_tokens = 100000`

The quota amount can be overridden with:

```env
INITIAL_MONTHLY_TOKEN_LIMIT=100000
```

## Existing users

On API startup, the backend backfills the same monthly quota for any existing user who does not already have a user-specific monthly `TokenLimit` with `agent_type = NULL`.

That means:

- new users are covered at signup time
- older users are patched automatically after the updated API starts
- users who already have a monthly combined limit are left unchanged

## Reliability notes

Completed run summaries are now persisted from the worker path instead of depending on an active SSE subscriber. This makes dashboard counts more stable for:

- `actions.extracted`
- `actions.normalized`
- `actions.executed`
- `integrationsFound`

## Example usage

```bash
curl -H "Authorization: Bearer $TOKEN" \
  http://localhost:8000/dashboard/summary
```
