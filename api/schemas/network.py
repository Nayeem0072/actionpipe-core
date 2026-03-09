"""Pydantic schemas for network API (people, teams, members)."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


# --- People ---


class PersonCreate(BaseModel):
    """Payload to create a person."""
    name: str = Field(..., min_length=1, max_length=255)
    email: str | None = Field(None, max_length=255)
    slack_handle: str | None = Field(None, max_length=128)
    notion_workspace: str | None = Field(None, max_length=255)
    jira_user: str | None = Field(None, max_length=128)
    jira_projects: list[str] | None = None
    is_client: bool = False


class PersonUpdate(BaseModel):
    """Payload to update a person (all fields optional)."""
    name: str | None = Field(None, min_length=1, max_length=255)
    email: str | None = Field(None, max_length=255)
    slack_handle: str | None = Field(None, max_length=128)
    notion_workspace: str | None = Field(None, max_length=255)
    jira_user: str | None = Field(None, max_length=128)
    jira_projects: list[str] | None = None
    is_client: bool | None = None
    user_id: UUID | None = Field(None, description="Link to a User (login account); same org. Set null to unlink.")


class PersonResponse(BaseModel):
    """Person in API responses."""
    id: UUID
    org_id: UUID
    name: str
    email: str | None
    slack_handle: str | None
    notion_workspace: str | None
    jira_user: str | None
    jira_projects: list[str] | None
    is_client: bool
    created_at: datetime
    user_id: UUID | None = None

    model_config = {"from_attributes": True}


class PersonWithTeamsResponse(PersonResponse):
    """Person with list of team ids they belong to."""
    team_ids: list[UUID] = []


# --- Teams ---


class TeamCreate(BaseModel):
    """Payload to create a team."""
    name: str = Field(..., min_length=1, max_length=255)
    email: str | None = Field(None, max_length=255)
    slack_handle: str | None = Field(None, max_length=128)
    slack_channel: str | None = Field(None, max_length=128)
    notion_workspace: str | None = Field(None, max_length=255)
    is_client: bool = False


class TeamUpdate(BaseModel):
    """Payload to update a team (all fields optional)."""
    name: str | None = Field(None, min_length=1, max_length=255)
    email: str | None = Field(None, max_length=255)
    slack_handle: str | None = Field(None, max_length=128)
    slack_channel: str | None = Field(None, max_length=128)
    notion_workspace: str | None = Field(None, max_length=255)
    is_client: bool | None = None


class TeamResponse(BaseModel):
    """Team in API responses."""
    id: UUID
    org_id: UUID
    name: str
    email: str | None
    slack_handle: str | None
    slack_channel: str | None
    notion_workspace: str | None
    is_client: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TeamWithMembersResponse(TeamResponse):
    """Team with list of member person ids."""
    member_ids: list[UUID] = []


# --- Members ---


class MemberAdd(BaseModel):
    """Payload to add a person to a team."""
    person_id: UUID


class MemberResponse(BaseModel):
    """Team membership in API responses."""
    team_id: UUID
    person_id: UUID
    created_at: datetime

    model_config = {"from_attributes": True}
