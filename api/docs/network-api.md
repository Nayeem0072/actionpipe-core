# Network API

REST API for managing an organization’s contact network: **people**, **teams**, and **team members**. Admins configure people (name, email, Slack, Notion, Jira), configure teams (with optional email, Slack channel, etc.), then assign people to teams. One person can belong to multiple teams. Both internal and **client** people/teams are supported.

**Base URL (local):** `http://localhost:8000`  
**Interactive docs:** `http://localhost:8000/docs`

All endpoints are **protected**: send JWT via `Authorization: Bearer <token>`. The current user’s `org_id` is used; you cannot access another org’s data.

---

## Overview

| Method | Path | Description |
|--------|------|-------------|
| **People** | | |
| `POST` | `/network/people` | Create a person (internal or client). |
| `GET` | `/network/people` | List people; optional `?is_client=true\|false`. |
| `GET` | `/network/people/{person_id}` | Get one person (with `team_ids`). |
| `PATCH` | `/network/people/{person_id}` | Update a person (partial). |
| `DELETE` | `/network/people/{person_id}` | Delete a person (removed from all teams). |
| **Teams** | | |
| `POST` | `/network/teams` | Create a team (internal or client). |
| `GET` | `/network/teams` | List teams; optional `?is_client=true\|false`. |
| `GET` | `/network/teams/{team_id}` | Get one team (with `member_ids`). |
| `PATCH` | `/network/teams/{team_id}` | Update a team (partial). |
| `DELETE` | `/network/teams/{team_id}` | Delete a team (all memberships removed). |
| **Members** | | |
| `GET` | `/network/teams/{team_id}/members` | List members of a team. |
| `POST` | `/network/teams/{team_id}/members` | Add a person to a team. |
| `DELETE` | `/network/teams/{team_id}/members/{person_id}` | Remove a person from a team. |
| **Graph** | | |
| `GET` | `/network/contacts` | Full contacts graph (same shape as `contacts.json`). |

---

## People

### POST /network/people

Create a person in the current org.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Display name (1–255 chars). |
| `email` | string | No | Email address. |
| `slack_handle` | string | No | e.g. `@priya`. |
| `notion_workspace` | string | No | Notion workspace identifier. |
| `jira_user` | string | No | Jira username. |
| `jira_projects` | array of string | No | Jira project keys. |
| `is_client` | boolean | No | `true` for client contact. Default `false`. |
| `user_id` | UUID or null | No | Link this person to a **User** (login account); must be same org. Set `null` to unlink. |

**Example:**

```json
{
  "name": "Priya",
  "email": "priya@company.com",
  "slack_handle": "@priya",
  "notion_workspace": "company-workspace",
  "jira_user": "priya",
  "jira_projects": ["PROJ"],
  "is_client": false
}
```

**Response:** `201 Created` — Person object (includes `id`, `org_id`, `created_at`).

---

### GET /network/people

List all people in the current org.

**Query parameters:**

| Name | Type | Description |
|------|------|-------------|
| `is_client` | boolean | If set, filter by `is_client` (`true` or `false`). |

**Response:** `200 OK` — Array of person objects, each including `team_ids` (list of team UUIDs the person belongs to).

---

### GET /network/people/{person_id}

Get a single person. Returns `404` if not in current org.

**Response:** Person object with `team_ids`.

---

### PATCH /network/people/{person_id}

Update a person. Only included fields are updated.

**Request body (JSON):** Same fields as create, all optional. Include `user_id` (UUID or `null`) to link or unlink a login account (see below).

**Response:** `200 OK` — Updated person (without `team_ids`).

---

### DELETE /network/people/{person_id}

Delete a person. They are removed from all teams. Returns `204 No Content` or `404`.

---

## Teams

### POST /network/teams

Create a team in the current org.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Team name (1–255 chars). |
| `email` | string | No | Team email. |
| `slack_handle` | string | No | Team Slack handle. |
| `slack_channel` | string | No | e.g. `#security`. |
| `notion_workspace` | string | No | Notion workspace. |
| `is_client` | boolean | No | `true` for client team. Default `false`. |

**Example:**

```json
{
  "name": "Security Team",
  "email": "security@company.com",
  "slack_channel": "#security",
  "is_client": false
}
```

**Response:** `201 Created` — Team object (includes `id`, `org_id`, `created_at`).

---

### GET /network/teams

List all teams. Optional query `is_client=true` or `is_client=false`.

**Response:** Array of team objects, each including `member_ids` (list of person UUIDs).

---

### GET /network/teams/{team_id}

Get a single team with `member_ids`. `404` if not in current org.

---

### PATCH /network/teams/{team_id}

Update a team. Only provided fields are updated. Response: updated team.

---

### DELETE /network/teams/{team_id}

Delete a team and all its member associations. `204 No Content` or `404`.

---

## Team members

### GET /network/teams/{team_id}/members

List members of a team (team must belong to current org).

**Response:** Array of `{ "team_id", "person_id", "created_at" }`.

---

### POST /network/teams/{team_id}/members

Add a person to a team.

**Request body (JSON):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `person_id` | UUID | Yes | Person to add (must be in same org). |

**Example:**

```json
{ "person_id": "550e8400-e29b-41d4-a716-446655440000" }
```

**Response:** `201 Created` — Membership object.  
**Errors:** `404` if team or person not found; `409 Conflict` if person is already a member.

---

### DELETE /network/teams/{team_id}/members/{person_id}

Remove a person from a team. `204 No Content` or `404` if membership or team not found.

---

## Contacts graph: GET /network/contacts

Returns the full contact network for the current org in the **same shape as `contacts.json`**: a single object with a `people` key, whose keys are person names and values are objects with `email`, `slack_handle`, `notion_workspace`, `jira_user`, `jira_projects`, and `connections`. Each `connections` entry is keyed by a normalized team name (e.g. `security_team`, `dev_team`) and contains team-level `email`, `slack_channel`, `slack_handle` as applicable.

**Response:** `200 OK` — JSON object, e.g.:

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
          "email": "security@company.com",
          "slack_channel": "#security"
        }
      }
    }
  }
}
```

Use this for the pipeline/executor or for exporting the network. Team names are normalized to a slug (lowercase, spaces → underscores) for connection keys.

---

## Errors

| Status | Condition |
|--------|-----------|
| `401` | Missing or invalid JWT (`Authorization: Bearer <token>`). |
| `404` | Person, team, or membership not found (or not in current org). |
| `409` | Person already a member of the team (POST member). |
| `503` | Auth0 not configured or JWKS unavailable. |

---

## Mapping users to org people

When someone **signs up** (creates a **User** via Auth0) but they were already added as a contact (**OrgPerson**), you can map them so the same identity is used for login and for the contact network.

- **Storage:** `org_people.user_id` is a nullable FK to `users.id`. At most one User can be linked to an OrgPerson (and each User can be linked to at most one OrgPerson in that org).
- **Automatic linking:** On every login, if the current user is not yet linked to an OrgPerson, the backend looks for an **unlinked** OrgPerson in the **same org** with the **same email** (case-insensitive). If exactly one is found, it sets `org_people.user_id = user.id`. So once the user is in the same org as their contact record, mapping happens automatically by email.
- **Same org requirement:** Auto-link only runs when the user’s org and the org person’s org match. New signups currently get a **new** organization by default, so they won’t be linked until they are placed in the existing org (e.g. via an **invite flow** or domain-based org assignment). When you add invite/join-org logic, new users who join an existing org will be auto-linked to any matching OrgPerson by email.
- **Manual linking:** An admin can set or clear the link via **PATCH /network/people/{person_id}** with `"user_id": "<user_id>"` or `"user_id": null`. The user must belong to the same org as the person; otherwise the API returns `400`.
- **Current user:** **GET /me** returns `org_person_id` when the authenticated user is linked to an OrgPerson, so the frontend can show “you’re in the network as …” or merge profile with contact data.

---

## Typical workflow

1. **Configure people** — `POST /network/people` for each person (internal and client), with name, email, Slack, Notion, Jira as needed.
2. **Configure teams** — `POST /network/teams` for each team (internal and client), with optional email, Slack channel, etc.
3. **Connect people to teams** — `POST /network/teams/{team_id}/members` with `{ "person_id": "..." }` for each membership.
4. **Export graph** — `GET /network/contacts` to get the `contacts.json`-shaped graph for the pipeline or other consumers.

All resources are scoped to the authenticated user’s organization; there is no `org_id` in the path.
