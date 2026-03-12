"""SQLAlchemy models for organizations, users, tokens, and org-level contacts."""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base for all models."""
    pass


class Organization(Base):
    """An organization; users belong to one org and share its contacts."""
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    users: Mapped[list["User"]] = relationship("User", back_populates="organization")
    org_contact: Mapped["OrgContact | None"] = relationship("OrgContact", back_populates="organization", uselist=False)
    org_people: Mapped[list["OrgPerson"]] = relationship("OrgPerson", back_populates="organization", cascade="all, delete-orphan")
    org_teams: Mapped[list["OrgTeam"]] = relationship("OrgTeam", back_populates="organization", cascade="all, delete-orphan")


class User(Base):
    """A user belonging to an organization; has login identity and linked tokens."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    auth0_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True, index=True)  # Auth0 "sub" claim
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    picture: Mapped[str | None] = mapped_column(String(512), nullable=True)  # Auth0 profile picture URL
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # for login; set when auth is added
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    organization: Mapped["Organization"] = relationship("Organization", back_populates="users")
    tokens: Mapped[list["UserToken"]] = relationship("UserToken", back_populates="user", cascade="all, delete-orphan")

    # Optional link to org_people when this user is the same as a contact in the network
    org_person: Mapped["OrgPerson | None"] = relationship(
        "OrgPerson", back_populates="user", uselist=False
    )

    __table_args__ = (UniqueConstraint("org_id", "email", name="uq_user_org_email"),)


class UserToken(Base):
    """Per-user access token for a service (Jira, Slack, Notion, etc.)."""
    __tablename__ = "user_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    service: Mapped[str] = mapped_column(String(64), nullable=False)  # e.g. jira, slack, notion
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)  # service-specific metadata
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    user: Mapped["User"] = relationship("User", back_populates="tokens")

    __table_args__ = (UniqueConstraint("user_id", "service", name="uq_user_token_service"),)


class OrgContact(Base):
    """One JSONB contacts graph per organization (same shape as contacts.json)."""
    __tablename__ = "org_contacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    contacts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    organization: Mapped["Organization"] = relationship("Organization", back_populates="org_contact")


class OrgPerson(Base):
    """A person in an org's network (internal or client). Can belong to multiple teams."""
    __tablename__ = "org_people"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slack_handle: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notion_workspace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    jira_user: Mapped[str | None] = mapped_column(String(128), nullable=True)
    jira_projects: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)  # list of project keys
    is_client: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # When set, this contact has a login account (User); must be same org
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        unique=True,
    )

    organization: Mapped["Organization"] = relationship("Organization", back_populates="org_people")
    user: Mapped["User | None"] = relationship("User", back_populates="org_person")
    team_memberships: Mapped[list["OrgTeamMember"]] = relationship(
        "OrgTeamMember", back_populates="person", cascade="all, delete-orphan"
    )


class OrgTeam(Base):
    """A team in an org's network (internal or client). Can have email, slack, etc."""
    __tablename__ = "org_teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    slack_handle: Mapped[str | None] = mapped_column(String(128), nullable=True)
    slack_channel: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notion_workspace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_client: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    organization: Mapped["Organization"] = relationship("Organization", back_populates="org_teams")
    members: Mapped[list["OrgTeamMember"]] = relationship(
        "OrgTeamMember", back_populates="team", cascade="all, delete-orphan"
    )


class OrgTeamMember(Base):
    """Association: a person belongs to a team. One person can be in many teams."""
    __tablename__ = "org_team_members"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("org_teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    person_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("org_people.id", ondelete="CASCADE"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    team: Mapped["OrgTeam"] = relationship("OrgTeam", back_populates="members")
    person: Mapped["OrgPerson"] = relationship("OrgPerson", back_populates="team_memberships")

    __table_args__ = (UniqueConstraint("team_id", "person_id", name="uq_org_team_member"),)


class RunRequestLog(Base):
    """Log entry for a run request (meeting metadata and inputs)."""
    __tablename__ = "run_request_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_auth0_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    meeting_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stored_file_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    responses: Mapped[list["RunResponseLog"]] = relationship(
        "RunResponseLog",
        back_populates="request",
        cascade="all, delete-orphan",
    )


class RunResponseLog(Base):
    """Log entry for a run response (outputs and status)."""
    __tablename__ = "run_response_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("run_request_logs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actions_extracted: Mapped[int | None] = mapped_column(nullable=True)
    actions_normalized: Mapped[int | None] = mapped_column(nullable=True)
    actions_executed: Mapped[int | None] = mapped_column(nullable=True)
    response_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    request: Mapped["RunRequestLog"] = relationship("RunRequestLog", back_populates="responses")


class AgentRunTask(Base):
    """Tracks each agent step (extractor/normalizer/executor) for a run.

    attempt_count is incremented each time a worker picks up the task. When it
    reaches max_attempts the status is set to permanently_failed and no further
    retries are attempted — even if the Celery task has retries remaining.
    The checkpoint_thread_id is stable across retries so LangGraph always resumes
    from the last completed node rather than starting over.
    """
    __tablename__ = "agent_run_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # extractor | normalizer | executor
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Stable key used with PostgresSaver: "{run_id}:{agent_type}"
    checkpoint_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # pending | running | completed | failed | permanently_failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (UniqueConstraint("run_id", "agent_type", name="uq_agent_run_task"),)


class TokenUsage(Base):
    """Per-agent token consumption record for a single run."""
    __tablename__ = "token_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # extractor | normalizer | executor
    agent_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class TokenLimit(Base):
    """Hard token budget per user (or global default) per period.

    If user_id is NULL the row is the global default applied to all users.
    If agent_type is NULL the limit applies to the combined total across all agents.
    A more specific row (non-null user_id or agent_type) takes precedence.
    """
    __tablename__ = "token_limits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # extractor | normalizer | executor | NULL (all agents combined)
    agent_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # daily | monthly
    period: Mapped[str] = mapped_column(String(16), nullable=False, default="daily")
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )
