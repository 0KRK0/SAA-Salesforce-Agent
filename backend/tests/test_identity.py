"""Enterprise identity: group mapping, session revocation, rate limiting, SCIM.

The property that matters most here is not "can someone log in" — it is **can
someone still get in after they should not be able to.** Every de-provisioning
path is tested from that direction.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    Company,
    CompanyMembership,
    CompanyRole,
    IdentityProviderKind,
    Project,
    ProjectMembership,
    ProjectRole,
    SSOConfiguration,
    User,
)
from app.security import mapping
from app.security.auth import create_session_token, revoke_sessions
from app.security.identity import RETAINED_CLAIMS
from app.security.ratelimit import RateLimiter, limit_for
from app.security.secrets import SecretContext, store_secret

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


def _headers(user: User, project_id: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_session_token(user.id, project_id, session_version=user.session_version)}",
        "X-Project-Id": project_id,
    }


@pytest.fixture
def auth(user, project):
    return _headers(user, project.id)


# ---------------------------------------------------------------------------
# Group claim extraction
# ---------------------------------------------------------------------------
def test_groups_are_found_in_whichever_claim_the_idp_used():
    """Entra, Okta and Auth0 all put groups somewhere different."""
    assert mapping.groups_from_claims({"groups": ["a", "b"]}) == ["a", "b"]
    assert mapping.groups_from_claims({"roles": ["x"]}) == ["x"]
    assert mapping.groups_from_claims({"memberOf": ["cn=Admins"]}) == ["cn=Admins"]


def test_a_single_group_sent_as_a_string_is_still_a_group():
    """Some IdPs collapse a one-element list. Ignoring that silently denies
    access to exactly the people who belong to one group."""
    assert mapping.groups_from_claims({"groups": "sf-admins"}) == ["sf-admins"]


def test_no_groups_is_an_empty_list_not_an_error():
    assert mapping.groups_from_claims(None) == []
    assert mapping.groups_from_claims({"email": "a@b.c"}) == []


def test_group_claims_survive_the_id_token_filter():
    """The OIDC provider discards most claims on the spot. If it discarded the
    group claims too, mapping would silently grant nothing to everyone."""
    for claim in mapping.GROUP_CLAIMS:
        assert claim in RETAINED_CLAIMS


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_a_group_maps_to_the_role_it_was_configured_for():
    result = mapping.resolve(
        ["sf-admins"],
        {"sf-admins": "SALESFORCE_ADMIN"},
        default_project_id="prj_1",
    )
    assert result.project_roles == {"prj_1": ProjectRole.SALESFORCE_ADMIN}
    assert result.matched_groups == ["sf-admins"]


def test_an_unrecognised_group_grants_nothing():
    """Unknown is not 'probably fine'. A typo in an IdP mapping must not become
    an accidental grant."""
    result = mapping.resolve(
        ["some-other-team"], {"sf-admins": "SALESFORCE_ADMIN"}, default_project_id="prj_1"
    )
    assert result.unmatched_groups == ["some-other-team"]
    # Falls back to the configured default, at least privilege.
    assert result.project_roles == {"prj_1": ProjectRole.VIEWER}
    assert result.fell_back_to_default is True


def test_the_stronger_role_wins_when_two_groups_map_to_one_project():
    """Being in both `developers` and `sf-admins` must not depend on dict order."""
    mappings = {"developers": "DEVELOPER", "sf-admins": "SALESFORCE_ADMIN"}
    forward = mapping.resolve(["developers", "sf-admins"], mappings, default_project_id="p")
    backward = mapping.resolve(["sf-admins", "developers"], mappings, default_project_id="p")
    assert forward.project_roles == backward.project_roles
    assert forward.project_roles["p"] is ProjectRole.SALESFORCE_ADMIN


def test_a_mapping_can_target_a_specific_project():
    result = mapping.resolve(
        ["release-team"],
        {"release-team": "prj_service:RELEASE_MANAGER"},
        default_project_id="prj_sales",
    )
    assert result.project_roles == {"prj_service": ProjectRole.RELEASE_MANAGER}


def test_one_group_can_grant_roles_in_several_projects():
    result = mapping.resolve(
        ["everyone"],
        {"everyone": ["prj_a:VIEWER", "prj_b:DEVELOPER"]},
    )
    assert result.project_roles == {
        "prj_a": ProjectRole.VIEWER,
        "prj_b": ProjectRole.DEVELOPER,
    }


def test_an_idp_group_cannot_grant_platform_ownership():
    """Operating this deployment is not something a customer's directory gets
    to hand out."""
    result = mapping.resolve(["admins"], {"admins": "PLATFORM_OWNER"})
    assert result.company_role is None


def test_a_group_can_grant_company_admin():
    result = mapping.resolve(["billing-owners"], {"billing-owners": "COMPANY_ADMIN"})
    assert result.company_role is CompanyRole.COMPANY_ADMIN


def test_an_unknown_role_name_is_ignored_rather_than_guessed():
    result = mapping.resolve(["team"], {"team": "SUPER_ADMIN"}, default_project_id="p")
    assert result.project_roles == {"p": ProjectRole.VIEWER}


def test_group_matching_is_case_insensitive():
    """IdPs are inconsistent about case, and a case-sensitive match fails in a
    way nobody can debug from the outside."""
    result = mapping.resolve(
        ["SF-Admins"], {"sf-admins": "SALESFORCE_ADMIN"}, default_project_id="p"
    )
    assert result.project_roles["p"] is ProjectRole.SALESFORCE_ADMIN


# ---------------------------------------------------------------------------
# Applying a mapping — the de-provisioning path
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def sso(db, company, project):
    company.sso_domains = ["acme.test"]
    config = SSOConfiguration(
        company_id=company.id,
        kind=IdentityProviderKind.OIDC,
        enabled=True,
        issuer="https://idp.test",
        client_id="cid",
        group_mappings={
            "sf-admins": "SALESFORCE_ADMIN",
            "devs": "DEVELOPER",
        },
        default_project_id=project.id,
        default_project_role=ProjectRole.VIEWER,
    )
    db.add(config)
    await db.commit()
    return config


@pytest_asyncio.fixture
async def sso_user(db):
    row = User(email="person@acme.test", idp_kind=IdentityProviderKind.OIDC)
    db.add(row)
    await db.commit()
    return row


async def test_a_login_grants_the_role_the_group_maps_to(db, sso, sso_user, project):
    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": ["sf-admins"]})
    await db.commit()

    from app.tenancy.service import project_membership

    membership = await project_membership(
        db, user_id=sso_user.id, project_id=project.id
    )
    assert membership.role is ProjectRole.SALESFORCE_ADMIN


async def test_losing_a_group_downgrades_on_the_next_login(db, sso, sso_user, project):
    """The whole point. A directory that can only add access is not a control."""
    from app.tenancy.service import project_membership

    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": ["sf-admins"]})
    await db.commit()
    assert (
        await project_membership(db, user_id=sso_user.id, project_id=project.id)
    ).role is ProjectRole.SALESFORCE_ADMIN

    # Removed from sf-admins in the IdP; still an employee.
    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": ["devs"]})
    await db.commit()
    assert (
        await project_membership(db, user_id=sso_user.id, project_id=project.id)
    ).role is ProjectRole.DEVELOPER


async def test_losing_every_group_deactivates_the_membership(
    db, sso, sso_user, project
):
    from sqlalchemy import select

    # An explicit project target, so removing the group has something to revoke.
    sso.group_mappings = {"sf-admins": f"{project.id}:SALESFORCE_ADMIN"}
    sso.default_project_id = None  # no fallback grant
    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": ["sf-admins"]})
    await db.commit()

    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": []})
    await db.commit()

    membership = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.user_id == sso_user.id,
                ProjectMembership.project_id == project.id,
            )
        )
    ).scalar_one()
    assert membership.is_active is False


async def test_a_hand_granted_membership_survives_an_sso_login(
    db, sso, sso_user, project, company
):
    """An invitation was not the directory's to grant, so it is not the
    directory's to take away. Otherwise one SSO login would silently revoke
    every contractor a project admin had added by hand."""
    from sqlalchemy import select

    db.add(
        ProjectMembership(
            company_id=company.id,
            project_id=project.id,
            user_id=sso_user.id,
            role=ProjectRole.DEVELOPER,
            external_id=None,  # granted by a person
        )
    )
    await db.commit()

    sso.default_project_id = None
    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": []})
    await db.commit()

    membership = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.user_id == sso_user.id,
                ProjectMembership.project_id == project.id,
            )
        )
    ).scalar_one()
    assert membership.is_active is True
    assert membership.role is ProjectRole.DEVELOPER


async def test_a_mapping_pointing_at_another_company_is_refused(
    db, sso, sso_user, company
):
    """A tenancy breach dressed as a configuration value."""
    from sqlalchemy import select

    other = Company(name="Not Yours", slug="not-yours-sso")
    db.add(other)
    await db.flush()
    foreign = Project(company_id=other.id, name="Theirs", slug="theirs-sso")
    db.add(foreign)
    await db.commit()

    sso.group_mappings = {"sneaky": f"{foreign.id}:PROJECT_ADMIN"}
    sso.default_project_id = None
    await mapping.apply(db, sso_user, sso, {"sub": "s1", "groups": ["sneaky"]})
    await db.commit()

    rows = (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == foreign.id
            )
        )
    ).scalars().all()
    assert rows == []


async def test_sso_login_finds_the_company_by_email_domain(db, sso, company):
    found = await mapping.config_for_email(db, "someone@acme.test")
    assert found is not None
    assert found.company_id == company.id
    assert await mapping.config_for_email(db, "someone@elsewhere.test") is None


# ---------------------------------------------------------------------------
# Session revocation
# ---------------------------------------------------------------------------
async def test_revoking_sessions_invalidates_tokens_already_issued(
    client, db, user, project
):
    """Not "expire the cookie" — a token already in someone's browser must stop
    working, including one this session cannot reach."""
    headers = _headers(user, project.id)
    assert (await client.get(f"{API}/auth/me", headers=headers)).status_code == 200

    await revoke_sessions(db, user)
    await db.commit()

    assert (await client.get(f"{API}/auth/me", headers=headers)).status_code == 401


async def test_a_fresh_login_after_revocation_works(client, db, user, project):
    """Revocation must not lock someone out permanently."""
    await revoke_sessions(db, user)
    await db.commit()
    fresh = _headers(user, project.id)
    assert (await client.get(f"{API}/auth/me", headers=fresh)).status_code == 200


async def test_a_deactivated_user_cannot_use_an_existing_session(
    client, db, user, project
):
    headers = _headers(user, project.id)
    user.is_active = False
    await db.commit()
    resp = await client.get(f"{API}/auth/me", headers=headers)
    assert resp.status_code == 401


async def test_revocation_does_not_reveal_why_a_session_failed(
    client, db, user, project
):
    """Disabled, revoked and nonexistent all answer the same. Distinguishing
    them hands an attacker three facts they did not have."""
    revoked = _headers(user, project.id)
    await revoke_sessions(db, user)
    await db.commit()
    revoked_detail = (await client.get(f"{API}/auth/me", headers=revoked)).json()

    ghost = {
        "Authorization": f"Bearer {create_session_token('usr_nonexistent', project.id)}",
        "X-Project-Id": project.id,
    }
    ghost_detail = (await client.get(f"{API}/auth/me", headers=ghost)).json()
    assert revoked_detail["detail"] == ghost_detail["detail"]


async def test_an_admin_can_revoke_another_members_sessions(client, db, project, user):
    member = User(email="leaver-sessions@example.com")
    db.add(member)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=project.company_id,
            project_id=project.id,
            user_id=member.id,
            role=ProjectRole.DEVELOPER,
        )
    )
    await db.commit()

    member_headers = _headers(member, project.id)
    assert (await client.get(f"{API}/auth/me", headers=member_headers)).status_code == 200

    revoked = await client.post(
        f"{API}/auth/projects/members/{member.id}/revoke-sessions",
        headers=_headers(user, project.id),
    )
    assert revoked.status_code == 200
    assert (
        await client.get(f"{API}/auth/me", headers=member_headers)
    ).status_code == 401


async def test_a_developer_cannot_revoke_someone_elses_sessions(client, db, project):
    developer = User(email="dev-revoke@example.com")
    target = User(email="target-revoke@example.com")
    db.add_all([developer, target])
    await db.flush()
    for u, role in ((developer, ProjectRole.DEVELOPER), (target, ProjectRole.DEVELOPER)):
        db.add(
            ProjectMembership(
                company_id=project.company_id,
                project_id=project.id,
                user_id=u.id,
                role=role,
            )
        )
    await db.commit()

    resp = await client.post(
        f"{API}/auth/projects/members/{target.id}/revoke-sessions",
        headers=_headers(developer, project.id),
    )
    assert resp.status_code == 403


async def test_a_token_without_a_version_claim_still_works(client, db, user, project):
    """Sessions minted before this feature existed must not all break on
    deploy. A missing `sv` is treated as version 1."""
    from datetime import UTC, datetime, timedelta

    from jose import jwt

    legacy = jwt.encode(
        {
            "sub": user.id,
            "prj": project.id,
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "iss": "sfagent",
        },
        settings.session_secret,
        algorithm="HS256",
    )
    resp = await client.get(
        f"{API}/auth/me",
        headers={"Authorization": f"Bearer {legacy}", "X-Project-Id": project.id},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------
def test_the_limiter_allows_up_to_the_limit_then_refuses():
    limiter = RateLimiter(window_seconds=60)
    for i in range(3):
        decision = limiter.check("k", 3, now=100.0 + i)
        assert decision.allowed
    blocked = limiter.check("k", 3, now=103.0)
    assert not blocked.allowed
    assert blocked.retry_after > 0


def test_the_window_slides_rather_than_resetting_on_the_minute():
    """A fixed window lets someone send 2N requests across a boundary."""
    limiter = RateLimiter(window_seconds=60)
    for i in range(3):
        limiter.check("k", 3, now=100.0 + i)
    assert not limiter.check("k", 3, now=150.0).allowed
    # The first hit ages out of the window.
    assert limiter.check("k", 3, now=161.0).allowed


def test_keys_are_independent():
    limiter = RateLimiter(window_seconds=60)
    for _ in range(3):
        limiter.check("a", 3, now=100.0)
    assert limiter.check("b", 3, now=100.0).allowed


def test_a_zero_limit_means_unlimited_not_blocked():
    """Otherwise setting a limit to 0 to disable it would lock everyone out."""
    limiter = RateLimiter()
    assert limiter.check("k", 0).allowed


def test_login_is_limited_more_tightly_than_ordinary_api_calls():
    login_name, login_limit = limit_for(f"{API}/auth/login")
    api_name, api_limit = limit_for(f"{API}/conversations")
    assert login_name == "rate_limit_login_per_minute"
    assert api_name == "rate_limit_api_per_minute"
    assert login_limit < api_limit


def test_starting_a_run_has_its_own_limit():
    """Each run spends money on a model and Salesforce API calls."""
    name, _ = limit_for(f"{API}/conversations/conv_1/messages")
    assert name == "rate_limit_run_start_per_minute"


def test_unauthenticated_routes_are_never_keyed_on_a_caller_supplied_cookie():
    """Otherwise an attacker resets their own login limit by rotating a junk
    cookie, and the limiter is evadable by exactly the traffic it stops."""
    from app.security.ratelimit import keys_on_session

    assert keys_on_session(f"{API}/auth/login") is False
    assert keys_on_session(f"{API}/auth/invitations/redeem") is False
    assert keys_on_session(f"{API}/scim/v2/Users") is False
    # Authenticated routes may key per session: fairer behind a shared NAT.
    assert keys_on_session(f"{API}/conversations") is True


def test_the_limiter_does_not_grow_without_bound():
    from app.security.ratelimit import MAX_BUCKETS

    limiter = RateLimiter(window_seconds=1)
    for i in range(MAX_BUCKETS + 100):
        limiter.check(f"key-{i}", 5, now=float(i))
    assert len(limiter._buckets) <= MAX_BUCKETS


def test_the_limiter_says_what_it_is_rather_than_implying_more():
    from app.security.ratelimit import describe

    described = describe()
    assert described["scope"] == "per application process"
    assert "N times" in described["note"]


async def test_a_flood_of_logins_is_refused_with_a_retry_after(client, monkeypatch):
    from app.security.ratelimit import limiter as global_limiter

    global_limiter.reset()
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 3)

    statuses = []
    for i in range(5):
        resp = await client.post(
            f"{API}/auth/login", json={"email": f"flood{i}@example.com"}
        )
        statuses.append(resp.status_code)
    global_limiter.reset()

    assert 429 in statuses
    assert statuses.index(429) >= 3


# ---------------------------------------------------------------------------
# SCIM
# ---------------------------------------------------------------------------
async def test_scim_accepts_a_companys_own_token(client, db, company, project):
    """The per-company token is what an enterprise customer configures in their
    IdP, and it must not reach another customer's projects."""
    token = "scim-token-for-acme"
    config = SSOConfiguration(
        company_id=company.id,
        kind=IdentityProviderKind.OIDC,
        scim_enabled=True,
        scim_token_ref=store_secret(
            token, SecretContext(company_id=company.id, purpose="scim_token")
        ),
    )
    db.add(config)
    await db.commit()

    resp = await client.get(
        f"{API}/scim/v2/Users",
        headers={"Authorization": f"Bearer {token}", "X-Project-Id": project.id},
    )
    assert resp.status_code == 200


async def test_one_companys_scim_token_cannot_provision_into_another(
    client, db, company, project
):
    other = Company(name="Rival SCIM", slug="rival-scim")
    db.add(other)
    await db.flush()
    other_project = Project(company_id=other.id, name="Theirs", slug="theirs-scim")
    db.add(other_project)
    db.add(
        SSOConfiguration(
            company_id=company.id,
            kind=IdentityProviderKind.OIDC,
            scim_enabled=True,
            scim_token_ref=store_secret(
                "acme-token", SecretContext(company_id=company.id, purpose="scim_token")
            ),
        )
    )
    await db.commit()

    resp = await client.get(
        f"{API}/scim/v2/Users",
        headers={
            "Authorization": "Bearer acme-token",
            "X-Project-Id": other_project.id,
        },
    )
    assert resp.status_code == 401


async def test_a_nonexistent_project_answers_like_a_bad_token(client, db):
    """Whether a project id exists is not something an unauthenticated caller
    gets to learn by watching status codes."""
    bad_token = await client.get(
        f"{API}/scim/v2/Users",
        headers={"Authorization": "Bearer nope", "X-Project-Id": "prj_real_looking"},
    )
    assert bad_token.status_code == 401


async def test_scim_without_any_token_is_unauthorized(client, project):
    resp = await client.get(
        f"{API}/scim/v2/Users", headers={"X-Project-Id": project.id}
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SSO configuration API
# ---------------------------------------------------------------------------
async def test_only_a_company_admin_can_configure_sso(client, db, project):
    member = User(email="member-sso@example.com")
    db.add(member)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=project.company_id,
            project_id=project.id,
            user_id=member.id,
            role=ProjectRole.PROJECT_ADMIN,
        )
    )
    db.add(
        CompanyMembership(
            company_id=project.company_id,
            user_id=member.id,
            role=CompanyRole.COMPANY_MEMBER,
        )
    )
    await db.commit()

    # Project admin is not company admin. That separation is the point.
    resp = await client.get(f"{API}/sso", headers=_headers(member, project.id))
    assert resp.status_code == 403


async def test_saml_cannot_be_enabled_while_it_is_not_implemented(client, auth):
    """Enabling it would leave a login method that cannot authenticate anyone."""
    resp = await client.put(
        f"{API}/sso",
        json={"kind": "SAML", "enabled": True},
        headers=auth,
    )
    assert resp.status_code == 400
    assert "not implemented" in resp.json()["detail"].lower()


async def test_oidc_cannot_be_enabled_without_what_it_needs(client, auth):
    resp = await client.put(
        f"{API}/sso", json={"kind": "OIDC", "enabled": True}, headers=auth
    )
    assert resp.status_code == 400
    assert "issuer" in resp.json()["detail"]


async def test_the_client_secret_never_comes_back(client, auth):
    secret = "oidc-client-secret-VALUE"
    created = await client.put(
        f"{API}/sso",
        json={
            "kind": "OIDC",
            "enabled": False,
            "issuer": "https://idp.test",
            "client_id": "cid",
            "client_secret": secret,
        },
        headers=auth,
    )
    assert created.status_code == 200
    assert secret not in created.text
    assert created.json()["has_client_secret"] is True

    listed = await client.get(f"{API}/sso", headers=auth)
    assert secret not in listed.text


async def test_a_mapping_to_another_companys_project_is_refused_at_configuration(
    client, auth, db
):
    other = Company(name="Elsewhere", slug="elsewhere-sso")
    db.add(other)
    await db.flush()
    foreign = Project(company_id=other.id, name="Theirs", slug="theirs-cfg")
    db.add(foreign)
    await db.commit()

    resp = await client.put(
        f"{API}/sso",
        json={
            "kind": "OIDC",
            "enabled": False,
            "group_mappings": {"admins": f"{foreign.id}:PROJECT_ADMIN"},
        },
        headers=auth,
    )
    assert resp.status_code == 404


async def test_the_scim_token_is_shown_once_and_never_again(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "feature_scim", True)
    minted = await client.post(f"{API}/sso/scim-token", headers=auth)
    assert minted.status_code == 200
    token = minted.json()["token"]
    assert token

    listed = await client.get(f"{API}/sso", headers=auth)
    assert token not in listed.text
    assert listed.json()["configurations"][0]["has_scim_token"] is True


async def test_mapping_preview_shows_what_a_login_would_grant(client, auth, db, project):
    """Group mappings fail silently: a typo grants nothing and nobody finds out
    until a person cannot get in."""
    await client.put(
        f"{API}/sso",
        json={
            "kind": "OIDC",
            "enabled": False,
            "group_mappings": {"sf-admins": "SALESFORCE_ADMIN"},
            "default_project_id": project.id,
        },
        headers=auth,
    )
    preview = await client.post(
        f"{API}/sso/preview", json=["sf-admins", "unknown-team"], headers=auth
    )
    body = preview.json()
    assert body["matched_groups"] == ["sf-admins"]
    assert body["unmatched_groups"] == ["unknown-team"]
    assert body["explained"][0]["role"] == "SALESFORCE_ADMIN"
    assert body["explained"][0]["project_name"] == project.name


# ---------------------------------------------------------------------------
# SCIM provisioning end to end
#
# The lifecycle an IdP actually drives. Before `project_memberships.external_id`
# existed these calls raised, because the SCIM handlers set and read a column
# that was not there — a defect no test reached, since nothing exercised the
# create path.
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def scim(db, company, project):
    token = "scim-lifecycle-token"
    db.add(
        SSOConfiguration(
            company_id=company.id,
            kind=IdentityProviderKind.OIDC,
            scim_enabled=True,
            scim_token_ref=store_secret(
                token, SecretContext(company_id=company.id, purpose="scim_token")
            ),
        )
    )
    await db.commit()
    return {
        "Authorization": f"Bearer {token}",
        "X-Project-Id": project.id,
        "Content-Type": "application/scim+json",
    }


async def test_scim_can_provision_a_user(client, scim):
    resp = await client.post(
        f"{API}/scim/v2/Users",
        json={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "userName": "provisioned@acme.test",
            "displayName": "Provisioned Person",
            "externalId": "okta-0001",
            "active": True,
            "roles": [{"value": "DEVELOPER", "primary": True}],
        },
        headers=scim,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["userName"] == "provisioned@acme.test"
    assert body["externalId"] == "okta-0001"
    assert body["roles"][0]["value"] == "DEVELOPER"


async def test_scim_deactivation_cuts_off_access_immediately(client, db, scim, project):
    """`active: false` is how an IdP cuts someone off. It is the operation that
    has to work on the day someone is let go."""
    created = (
        await client.post(
            f"{API}/scim/v2/Users",
            json={"userName": "leaver@acme.test", "roles": [{"value": "DEVELOPER"}]},
            headers=scim,
        )
    ).json()
    user_id = created["id"]

    patched = await client.patch(
        f"{API}/scim/v2/Users/{user_id}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": False}],
        },
        headers=scim,
    )
    assert patched.status_code == 200
    assert patched.json()["active"] is False

    # And the session layer agrees: an inactive membership is not a membership.
    from app.tenancy.service import project_membership

    assert (
        await project_membership(db, user_id=user_id, project_id=project.id)
    ) is None


async def test_scim_refuses_a_role_it_does_not_recognise(client, scim):
    """A typo in an IdP role mapping must be an error, not a silent downgrade
    to whatever seemed closest."""
    resp = await client.post(
        f"{API}/scim/v2/Users",
        json={"userName": "typo@acme.test", "roles": [{"value": "SUPERADMIN"}]},
        headers=scim,
    )
    assert resp.status_code == 400
    assert "Unknown role" in str(resp.json()["detail"])


async def test_scim_assigns_least_privilege_when_the_idp_asserts_no_role(
    client, scim
):
    resp = await client.post(
        f"{API}/scim/v2/Users", json={"userName": "nobody@acme.test"}, headers=scim
    )
    assert resp.json()["roles"][0]["value"] == ProjectRole.VIEWER.value


async def test_scim_deprovisioning_keeps_the_user_for_audit(client, db, scim):
    """Removing the membership revokes access. Deleting the user would erase
    who did what, which is the opposite of what a compliance de-provision
    wants."""
    from sqlalchemy import select

    created = (
        await client.post(
            f"{API}/scim/v2/Users",
            json={"userName": "gone@acme.test", "roles": [{"value": "DEVELOPER"}]},
            headers=scim,
        )
    ).json()

    deleted = await client.delete(
        f"{API}/scim/v2/Users/{created['id']}", headers=scim
    )
    assert deleted.status_code == 204

    still_there = (
        await db.execute(select(User).where(User.id == created["id"]))
    ).scalar_one_or_none()
    assert still_there is not None


async def test_scim_says_plainly_what_it_does_not_implement(client, scim):
    config = await client.get(f"{API}/scim/v2/ServiceProviderConfig", headers=scim)
    body = config.json()
    assert body["implementedResources"] == ["Users"]
    assert "Groups" in body["notImplemented"]
