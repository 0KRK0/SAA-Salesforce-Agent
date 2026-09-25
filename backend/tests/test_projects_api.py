"""Company, project, membership and invitation endpoints.

These are the surfaces where a mistake grants someone standing they should not
have, so each test names the authority it is checking rather than the route.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    CompanyMembership,
    CompanyRole,
    Invitation,
    ProjectMembership,
    ProjectRole,
    User,
)
from app.security.auth import create_session_token

API = settings.api_v1


@pytest_asyncio.fixture
async def client(db):
    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _headers(user_id: str, project_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_session_token(user_id, project_id)}",
        "X-Project-Id": project_id,
    }


@pytest.fixture
def auth(user, project):
    return _headers(user.id, project.id)


async def _member(db, project, email: str, role: ProjectRole) -> User:
    row = User(email=email)
    db.add(row)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=project.company_id,
            project_id=project.id,
            user_id=row.id,
            role=role,
        )
    )
    db.add(
        CompanyMembership(
            company_id=project.company_id,
            user_id=row.id,
            role=CompanyRole.COMPANY_MEMBER,
        )
    )
    await db.commit()
    return row


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
async def test_me_reports_both_the_project_and_company_role(client, auth, project):
    body = (await client.get(f"{API}/auth/me", headers=auth)).json()
    assert body["project_id"] == project.id
    assert body["company_id"] == project.company_id
    assert body["role"] == ProjectRole.PROJECT_ADMIN.value
    assert body["company_role"] == CompanyRole.COMPANY_ADMIN.value


async def test_login_lands_a_brand_new_user_somewhere_usable(client, db):
    """Self-serve signup has to arrive in a real company and project, or the
    very next request has nothing to scope to."""
    resp = await client.post(
        f"{API}/auth/login", json={"email": "newcomer@example.com"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["company_id"]
    assert body["project_id"]
    assert body["role"] == ProjectRole.PROJECT_ADMIN.value


async def test_project_listing_shows_only_projects_you_belong_to(
    client, auth, db, project, user
):
    from app.tenancy.service import create_company, create_project

    # A whole other customer, with a project the caller is not a member of.
    stranger = User(email="stranger@example.com")
    db.add(stranger)
    await db.flush()
    other_company, _ = await create_company(db, name="Somebody Else", owner=stranger)
    hidden, _ = await create_project(
        db, company_id=other_company.id, name="Hidden", owner=stranger
    )
    await db.commit()

    body = (await client.get(f"{API}/auth/projects", headers=auth)).json()
    ids = {p["id"] for p in body["projects"]}
    assert project.id in ids
    assert hidden.id not in ids


# ---------------------------------------------------------------------------
# Project creation
# ---------------------------------------------------------------------------
async def test_only_a_company_admin_can_create_a_project(client, db, project):
    """A project is a security boundary. Anyone who can mint one could
    otherwise mint themselves an unpoliced one."""
    developer = await _member(db, project, "dev@example.com", ProjectRole.DEVELOPER)
    resp = await client.post(
        f"{API}/auth/projects",
        json={"name": "Shadow project"},
        headers=_headers(developer.id, project.id),
    )
    assert resp.status_code == 403


async def test_the_plan_limit_on_projects_is_enforced(client, auth, db, project):
    """The trial plan includes one project; the second attempt must be refused
    rather than quietly allowed."""
    resp = await client.post(
        f"{API}/auth/projects", json={"name": "Second project"}, headers=auth
    )
    assert resp.status_code == 402
    assert "plan" in resp.json()["detail"].lower()


async def test_a_company_admin_can_create_a_project_within_the_plan(
    client, auth, db, project
):
    from app.tenancy.service import subscription_for

    subscription = await subscription_for(db, project.company_id)
    subscription.max_projects = 5
    await db.commit()

    resp = await client.post(
        f"{API}/auth/projects", json={"name": "Service Cloud"}, headers=auth
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == ProjectRole.PROJECT_ADMIN.value


# ---------------------------------------------------------------------------
# Switching
# ---------------------------------------------------------------------------
async def test_switching_to_a_project_you_do_not_belong_to_reads_as_not_found(
    client, auth, db, user
):
    from app.tenancy.service import create_company, create_project

    stranger = User(email="stranger2@example.com")
    db.add(stranger)
    await db.flush()
    other, _ = await create_company(db, name="Not Yours", owner=stranger)
    hidden, _ = await create_project(
        db, company_id=other.id, name="Hidden", owner=stranger
    )
    await db.commit()

    resp = await client.post(
        f"{API}/auth/projects/switch", json={"project_id": hidden.id}, headers=auth
    )
    # 404, not 403: the API never confirms that another tenant's project exists.
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Membership
# ---------------------------------------------------------------------------
async def test_only_a_project_admin_can_add_members(client, db, project):
    developer = await _member(db, project, "dev2@example.com", ProjectRole.DEVELOPER)
    resp = await client.post(
        f"{API}/auth/projects/members",
        json={"email": "recruit@example.com", "role": "PROJECT_ADMIN"},
        headers=_headers(developer.id, project.id),
    )
    assert resp.status_code == 403


async def test_adding_a_member_also_places_them_in_the_company(client, auth, db, project):
    from sqlalchemy import select

    resp = await client.post(
        f"{API}/auth/projects/members",
        json={"email": "recruit2@example.com", "role": "DEVELOPER"},
        headers=auth,
    )
    assert resp.status_code == 200
    user_id = resp.json()["user_id"]
    company_membership = (
        await db.execute(
            select(CompanyMembership).where(
                CompanyMembership.company_id == project.company_id,
                CompanyMembership.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    assert company_membership is not None
    # ...at the lowest company authority. Joining a project must not confer
    # company-wide standing.
    assert company_membership.role is CompanyRole.COMPANY_MEMBER


async def test_an_admin_cannot_remove_themselves(client, auth, project):
    """Otherwise a project can be left with no administrator at all."""
    body = (await client.get(f"{API}/auth/me", headers=auth)).json()
    resp = await client.delete(
        f"{API}/auth/projects/members/{body['id']}", headers=auth
    )
    assert resp.status_code == 400


async def test_removing_a_member_revokes_their_access(client, auth, db, project):
    member = await _member(db, project, "leaver@example.com", ProjectRole.DEVELOPER)
    removed = await client.delete(
        f"{API}/auth/projects/members/{member.id}", headers=auth
    )
    assert removed.status_code == 200

    resp = await client.get(
        f"{API}/conversations", headers=_headers(member.id, project.id)
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------
async def test_the_invitation_token_is_returned_once_and_stored_only_as_a_hash(
    client, auth, db
):
    from sqlalchemy import select

    resp = await client.post(
        f"{API}/auth/invitations",
        json={"email": "invited@example.com", "role": "DEVELOPER"},
        headers=auth,
    )
    assert resp.status_code == 201
    token = resp.json()["invite_url"].rsplit("/", 1)[-1]

    row = (await db.execute(select(Invitation))).scalars().one()
    assert token not in row.token_hash
    assert len(row.token_hash) == 64


async def test_an_invitation_grants_exactly_the_role_it_was_issued_for(
    client, auth, db, project
):
    created = await client.post(
        f"{API}/auth/invitations",
        json={"email": "invited2@example.com", "role": "AUDITOR"},
        headers=auth,
    )
    token = created.json()["invite_url"].rsplit("/", 1)[-1]

    redeemed = await client.post(
        f"{API}/auth/invitations/redeem",
        json={"token": token, "email": "invited2@example.com"},
    )
    assert redeemed.status_code == 200
    body = redeemed.json()
    assert body["project_id"] == project.id
    assert body["role"] == ProjectRole.AUDITOR.value


async def test_an_invitation_cannot_be_redeemed_by_a_different_person(client, auth, db):
    """An invitation link is not a bearer token for the project — it is bound to
    the address it was issued to."""
    created = await client.post(
        f"{API}/auth/invitations",
        json={"email": "intended@example.com", "role": "PROJECT_ADMIN"},
        headers=auth,
    )
    token = created.json()["invite_url"].rsplit("/", 1)[-1]

    resp = await client.post(
        f"{API}/auth/invitations/redeem",
        json={"token": token, "email": "interloper@example.com"},
    )
    assert resp.status_code == 400
    # The error must not confirm who it was for.
    assert "intended@example.com" not in resp.json()["detail"]


async def test_an_invitation_is_single_use(client, auth, db):
    created = await client.post(
        f"{API}/auth/invitations",
        json={"email": "once@example.com", "role": "DEVELOPER"},
        headers=auth,
    )
    token = created.json()["invite_url"].rsplit("/", 1)[-1]
    payload = {"token": token, "email": "once@example.com"}

    assert (
        await client.post(f"{API}/auth/invitations/redeem", json=payload)
    ).status_code == 200
    second = await client.post(f"{API}/auth/invitations/redeem", json=payload)
    assert second.status_code == 400
    assert "already been used" in second.json()["detail"]


async def test_an_expired_invitation_is_refused(client, auth, db):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    created = await client.post(
        f"{API}/auth/invitations",
        json={"email": "stale@example.com", "role": "DEVELOPER"},
        headers=auth,
    )
    token = created.json()["invite_url"].rsplit("/", 1)[-1]

    row = (await db.execute(select(Invitation))).scalars().one()
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db.commit()

    resp = await client.post(
        f"{API}/auth/invitations/redeem",
        json={"token": token, "email": "stale@example.com"},
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()


async def test_a_forged_invitation_token_is_refused(client):
    resp = await client.post(
        f"{API}/auth/invitations/redeem",
        json={"token": "made-up-token", "email": "nobody@example.com"},
    )
    assert resp.status_code == 400


async def test_only_a_project_admin_can_issue_invitations(client, db, project):
    developer = await _member(db, project, "dev3@example.com", ProjectRole.DEVELOPER)
    resp = await client.post(
        f"{API}/auth/invitations",
        json={"email": "someone@example.com", "role": "PROJECT_ADMIN"},
        headers=_headers(developer.id, project.id),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Entitlements
# ---------------------------------------------------------------------------
async def test_the_subscription_endpoint_does_not_claim_billing_it_does_not_have(
    client, auth
):
    body = (await client.get(f"{API}/project/subscription", headers=auth)).json()
    assert body["plan"]
    assert body["billing"]["provider_connected"] is False
    assert "not implemented" in body["billing"]["note"].lower()
