"""
Network API: manage org people, teams, and team members (contacts graph).

  People:  POST/GET /network/people, GET/PATCH/DELETE /network/people/{id}
  Teams:   POST/GET /network/teams, GET/PATCH/DELETE /network/teams/{id}
  Members: GET/POST /network/teams/{team_id}/members, DELETE /network/teams/{team_id}/members/{person_id}
  Graph:   GET /network/contacts — full contacts.json shape for the org
"""
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Annotated

from api.auth import get_current_user
from api.db import get_db
from api.models import User, OrgPerson, OrgTeam, OrgTeamMember, OrgContact
from api.schemas.network import (
    PersonCreate,
    PersonUpdate,
    PersonResponse,
    PersonWithTeamsResponse,
    TeamCreate,
    TeamUpdate,
    TeamResponse,
    TeamWithMembersResponse,
    MemberAdd,
    MemberResponse,
)

router = APIRouter(prefix="/network", tags=["network"])


def _team_slug(name: str) -> str:
    """Normalize team name for use as connection key (e.g. 'Dev Team' -> 'dev_team')."""
    return name.strip().lower().replace(" ", "_").replace("-", "_") or "team"


def build_contacts_graph(people: list[OrgPerson], teams: list[OrgTeam], members: list[OrgTeamMember]) -> dict:
    """
    Build the contacts.json-shaped graph: people keyed by name, each with connections to teams.
    Team slug (normalized name) is used as connection key; each connection has team-level email/slack_channel.
    """
    team_by_id = {t.id: t for t in teams}
    # person_id -> set of team_ids
    person_teams: dict[UUID, set[UUID]] = {}
    for m in members:
        person_teams.setdefault(m.person_id, set()).add(m.team_id)

    people_out: dict[str, dict] = {}
    for p in people:
        conn: dict[str, dict] = {}
        for tid in person_teams.get(p.id, []):
            t = team_by_id.get(tid)
            if not t:
                continue
            slug = _team_slug(t.name)
            entry: dict = {}
            if t.email:
                entry["email"] = t.email
            if t.slack_channel:
                entry["slack_channel"] = t.slack_channel
            if t.slack_handle:
                entry["slack_handle"] = t.slack_handle
            conn[slug] = entry

        people_out[p.name] = {
            "email": p.email,
            "slack_handle": p.slack_handle,
            "notion_workspace": p.notion_workspace,
            "jira_user": p.jira_user,
            "jira_projects": p.jira_projects,
            "connections": conn,
        }
        # Drop keys with None
        people_out[p.name] = {k: v for k, v in people_out[p.name].items() if v is not None}

    return {"people": people_out}


async def sync_org_contacts(
    db: AsyncSession,
    org_id: UUID,
    *,
    graph: dict | None = None,
) -> None:
    """
    Rebuild the contacts graph from org_people, org_teams, org_team_members
    and upsert it into org_contacts so the table stays in sync.
    If graph is provided, use it instead of loading from DB.
    """
    if graph is None:
        people_result = await db.execute(select(OrgPerson).where(OrgPerson.org_id == org_id))
        people = list(people_result.scalars().all())
        teams_result = await db.execute(select(OrgTeam).where(OrgTeam.org_id == org_id))
        teams = list(teams_result.scalars().all())
        members_result = await db.execute(
            select(OrgTeamMember).where(OrgTeamMember.team_id.in_([t.id for t in teams]))
        )
        members = list(members_result.scalars().all())
        graph = build_contacts_graph(people, teams, members)
    existing = await db.execute(select(OrgContact).where(OrgContact.org_id == org_id))
    row = existing.scalars().first()
    if row:
        row.contacts = graph
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(OrgContact(org_id=org_id, contacts=graph))
    await db.flush()


# --- People ---


@router.post("/people", status_code=status.HTTP_201_CREATED, response_model=PersonResponse)
async def create_person(
    body: PersonCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Create a person in the current org (internal or client)."""
    person = OrgPerson(
        org_id=user.org_id,
        name=body.name,
        email=body.email,
        slack_handle=body.slack_handle,
        notion_workspace=body.notion_workspace,
        jira_user=body.jira_user,
        jira_projects=body.jira_projects,
        is_client=body.is_client,
    )
    db.add(person)
    await db.flush()
    await db.refresh(person)
    await sync_org_contacts(db, user.org_id)
    return person


@router.get("/people", response_model=list[PersonWithTeamsResponse])
async def list_people(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    is_client: bool | None = None,
):
    """List all people in the current org. Optionally filter by is_client."""
    q = select(OrgPerson).where(OrgPerson.org_id == user.org_id)
    if is_client is not None:
        q = q.where(OrgPerson.is_client == is_client)
    q = q.options(selectinload(OrgPerson.team_memberships))
    result = await db.execute(q)
    people = list(result.scalars().unique().all())
    out = []
    for p in people:
        data = PersonResponse.model_validate(p)
        team_ids = [m.team_id for m in p.team_memberships]
        out.append(PersonWithTeamsResponse(**data.model_dump(), team_ids=team_ids))
    return out


@router.get("/people/{person_id}", response_model=PersonWithTeamsResponse)
async def get_person(
    person_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Get a person by id. Must belong to current org."""
    result = await db.execute(
        select(OrgPerson)
        .where(OrgPerson.id == person_id, OrgPerson.org_id == user.org_id)
        .options(selectinload(OrgPerson.team_memberships))
    )
    person = result.scalars().unique().first()
    if not person:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    data = PersonResponse.model_validate(person)
    team_ids = [m.team_id for m in person.team_memberships]
    return PersonWithTeamsResponse(**data.model_dump(), team_ids=team_ids)


@router.patch("/people/{person_id}", response_model=PersonResponse)
async def update_person(
    person_id: UUID,
    body: PersonUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Update a person. Only provided fields are updated."""
    result = await db.execute(
        select(OrgPerson).where(OrgPerson.id == person_id, OrgPerson.org_id == user.org_id)
    )
    person = result.scalars().first()
    if not person:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    updates = body.model_dump(exclude_unset=True)
    if "user_id" in updates:
        uid = updates["user_id"]
        if uid is not None:
            u = await db.get(User, uid)
            if not u or u.org_id != user.org_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="user_id must be a user in the same organization",
                )
        # else: unlink (person.user_id = None) is allowed
    for k, v in updates.items():
        setattr(person, k, v)
    await db.flush()
    await db.refresh(person)
    await sync_org_contacts(db, user.org_id)
    return person


@router.delete("/people/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_person(
    person_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Delete a person and remove them from all teams."""
    result = await db.execute(
        select(OrgPerson).where(OrgPerson.id == person_id, OrgPerson.org_id == user.org_id)
    )
    person = result.scalars().first()
    if not person:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    await db.delete(person)
    await sync_org_contacts(db, user.org_id)
    return None


# --- Teams ---


@router.post("/teams", status_code=status.HTTP_201_CREATED, response_model=TeamResponse)
async def create_team(
    body: TeamCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Create a team in the current org (internal or client)."""
    team = OrgTeam(
        org_id=user.org_id,
        name=body.name,
        email=body.email,
        slack_handle=body.slack_handle,
        slack_channel=body.slack_channel,
        notion_workspace=body.notion_workspace,
        is_client=body.is_client,
    )
    db.add(team)
    await db.flush()
    await db.refresh(team)
    await sync_org_contacts(db, user.org_id)
    return team


@router.get("/teams", response_model=list[TeamWithMembersResponse])
async def list_teams(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    is_client: bool | None = None,
):
    """List all teams in the current org. Optionally filter by is_client."""
    q = select(OrgTeam).where(OrgTeam.org_id == user.org_id)
    if is_client is not None:
        q = q.where(OrgTeam.is_client == is_client)
    q = q.options(selectinload(OrgTeam.members))
    result = await db.execute(q)
    teams = list(result.scalars().unique().all())
    out = []
    for t in teams:
        data = TeamResponse.model_validate(t)
        member_ids = [m.person_id for m in t.members]
        out.append(TeamWithMembersResponse(**data.model_dump(), member_ids=member_ids))
    return out


@router.get("/teams/{team_id}", response_model=TeamWithMembersResponse)
async def get_team(
    team_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Get a team by id. Must belong to current org."""
    result = await db.execute(
        select(OrgTeam)
        .where(OrgTeam.id == team_id, OrgTeam.org_id == user.org_id)
        .options(selectinload(OrgTeam.members))
    )
    team = result.scalars().unique().first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    data = TeamResponse.model_validate(team)
    member_ids = [m.person_id for m in team.members]
    return TeamWithMembersResponse(**data.model_dump(), member_ids=member_ids)


@router.patch("/teams/{team_id}", response_model=TeamResponse)
async def update_team(
    team_id: UUID,
    body: TeamUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Update a team. Only provided fields are updated."""
    result = await db.execute(
        select(OrgTeam).where(OrgTeam.id == team_id, OrgTeam.org_id == user.org_id)
    )
    team = result.scalars().first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    updates = body.model_dump(exclude_unset=True)
    for k, v in updates.items():
        setattr(team, k, v)
    await db.flush()
    await db.refresh(team)
    await sync_org_contacts(db, user.org_id)
    return team


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Delete a team and remove all member associations."""
    result = await db.execute(
        select(OrgTeam).where(OrgTeam.id == team_id, OrgTeam.org_id == user.org_id)
    )
    team = result.scalars().first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    await db.delete(team)
    await sync_org_contacts(db, user.org_id)
    return None


# --- Team members ---


@router.get("/teams/{team_id}/members", response_model=list[MemberResponse])
async def list_team_members(
    team_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """List members of a team. Team must belong to current org."""
    result = await db.execute(
        select(OrgTeam).where(OrgTeam.id == team_id, OrgTeam.org_id == user.org_id)
    )
    team = result.scalars().first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    result = await db.execute(
        select(OrgTeamMember).where(OrgTeamMember.team_id == team_id)
    )
    members = result.scalars().all()
    return [MemberResponse.model_validate(m) for m in members]


@router.post("/teams/{team_id}/members", status_code=status.HTTP_201_CREATED, response_model=MemberResponse)
async def add_team_member(
    team_id: UUID,
    body: MemberAdd,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Add a person to a team. Person and team must belong to current org."""
    team_result = await db.execute(
        select(OrgTeam).where(OrgTeam.id == team_id, OrgTeam.org_id == user.org_id)
    )
    team = team_result.scalars().first()
    if not team:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    person_result = await db.execute(
        select(OrgPerson).where(OrgPerson.id == body.person_id, OrgPerson.org_id == user.org_id)
    )
    if not person_result.scalars().first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person not found")
    existing = await db.execute(
        select(OrgTeamMember).where(
            OrgTeamMember.team_id == team_id,
            OrgTeamMember.person_id == body.person_id,
        )
    )
    if existing.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Person is already a member of this team",
        )
    member = OrgTeamMember(team_id=team_id, person_id=body.person_id)
    db.add(member)
    await db.flush()
    await db.refresh(member)
    await sync_org_contacts(db, user.org_id)
    return member


@router.delete("/teams/{team_id}/members/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_team_member(
    team_id: UUID,
    person_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
):
    """Remove a person from a team."""
    result = await db.execute(
        select(OrgTeamMember).where(
            OrgTeamMember.team_id == team_id,
            OrgTeamMember.person_id == person_id,
        )
    )
    member = result.scalars().first()
    if not member:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team membership not found")
    team_result = await db.execute(
        select(OrgTeam).where(OrgTeam.id == team_id, OrgTeam.org_id == user.org_id)
    )
    if not team_result.scalars().first():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    await db.delete(member)
    await sync_org_contacts(db, user.org_id)
    return None


# --- Contacts graph (contacts.json shape) ---


@router.get("/contacts")
async def get_contacts(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """
    Return the full contacts graph for the current org in the same shape as contacts.json.
    Use this for the pipeline/executor or for exporting the network.
    """
    people_result = await db.execute(select(OrgPerson).where(OrgPerson.org_id == user.org_id))
    people = list(people_result.scalars().all())
    teams_result = await db.execute(select(OrgTeam).where(OrgTeam.org_id == user.org_id))
    teams = list(teams_result.scalars().all())
    members_result = await db.execute(
        select(OrgTeamMember).where(
            OrgTeamMember.team_id.in_([t.id for t in teams]),
        )
    )
    members = list(members_result.scalars().all())
    graph = build_contacts_graph(people, teams, members)
    await sync_org_contacts(db, user.org_id, graph=graph)
    return graph
