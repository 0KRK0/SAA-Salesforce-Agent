"""Whose Salesforce Connected App is it?

A Connected App — an *External Client App* since Spring '26 — is not an
implementation detail of the vendor. It is the control a Salesforce
administrator uses to decide which profiles may reach their org through an
integration, from which IP ranges, for how long, and it carries the one button
that revokes every session at once.

Holding a single shared app in the deployment's environment variables makes all
of that the vendor's rather than the customer's, and couples every tenant to
one revocation: a single customer's security team pulling the app takes every
other customer offline with it.

So the properties under test here are:

  * a company's own app wins over the deployment's, and a project's over the
    company's;
  * the consumer secret goes to the secret store and no endpoint returns it;
  * a handshake and a connection remember *which* app authorised them, because
    the token exchange must present the same client_id the authorize URL did
    and a refresh must go back to the app that issued the token;
  * one company cannot see, use, or scope an app to another company's project.

And one that is not about multi-tenancy at all: a callback URL that does not
match a mounted route must be refused *before* the round-trip. That defect
sends a person through a completely successful Salesforce login and drops them
on a 404 holding a valid authorization code — it looks like Salesforce broke,
and it is entirely ours.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import AuditEvent, Company, CompanyMembership, CompanyRole, SalesforceApp, User
from app.security.auth import create_session_token
from app.security.secrets import SecretContext, resolve_secret

API = settings.api_v1

CONSUMER_KEY = "3MVG9abcdefghijklmnopqrstuvwxyz0123456789"
CONSUMER_SECRET = "8675309ABCDEF0123456789abcdef"


@pytest_asyncio.fixture
async def client(db):
    async def _override():
        yield db

    app.dependency_overrides[get_session] = _override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def auth(user, project):
    return {
        "Authorization": f"Bearer {create_session_token(user.id, project.id)}",
        "X-Project-Id": project.id,
    }


def _payload(**overrides):
    body = {
        "name": "Acme production app",
        "client_id": CONSUMER_KEY,
        "client_secret": CONSUMER_SECRET,
        "login_url": "https://login.salesforce.com",
        "is_default": True,
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Registering one
# ---------------------------------------------------------------------------
async def test_a_company_registers_its_own_app(client, auth):
    resp = await client.post(f"{API}/salesforce/apps", headers=auth, json=_payload())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["client_id"] == CONSUMER_KEY
    assert body["scope"] == "company"
    assert body["has_client_secret"] is True


async def test_the_consumer_secret_is_never_returned_by_any_endpoint(client, auth):
    """The consumer key travels in every authorize URL and is shown. The secret
    is the credential, and there is no endpoint that reads it back."""
    created = (
        await client.post(f"{API}/salesforce/apps", headers=auth, json=_payload())
    ).json()
    listed = (await client.get(f"{API}/salesforce/apps", headers=auth)).json()
    config = (await client.get(f"{API}/salesforce/config", headers=auth)).json()

    for blob in (created, listed, config):
        assert CONSUMER_SECRET not in str(blob)
    # ...but an administrator can still confirm *which* secret is in place.
    assert created["client_secret_fingerprint"]


async def test_the_secret_is_held_by_the_secret_store_not_the_column(client, auth, db, company):
    await client.post(f"{API}/salesforce/apps", headers=auth, json=_payload())
    row = (await db.execute(select(SalesforceApp))).scalars().one()

    assert CONSUMER_SECRET not in (row.client_secret_ref or "")
    assert resolve_secret(
        row.client_secret_ref,
        SecretContext(company_id=company.id, purpose="salesforce_client_secret"),
    ) == CONSUMER_SECRET


async def test_registering_an_app_is_audited_without_the_secret(client, auth, db):
    await client.post(f"{API}/salesforce/apps", headers=auth, json=_payload())
    event = (
        await db.execute(
            select(AuditEvent).where(AuditEvent.action == "salesforce.app_registered")
        )
    ).scalars().one()

    assert event.arguments["client_id"] == CONSUMER_KEY
    assert event.arguments["secret_provided"] is True
    assert CONSUMER_SECRET not in str(event.arguments)


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------
async def test_a_company_app_beats_the_deployment_app(db, company, project, monkeypatch):
    monkeypatch.setattr(settings, "salesforce_client_id", "deployment-key")
    from app.salesforce.apps import resolve_client

    db.add(
        SalesforceApp(
            company_id=company.id, name="Theirs", client_id=CONSUMER_KEY, is_default=True
        )
    )
    await db.commit()

    client = await resolve_client(db, company_id=company.id, project_id=project.id)
    assert client.client_id == CONSUMER_KEY
    assert client.source == "company"


async def test_a_project_app_beats_the_company_app(db, company, project):
    """One team pointing at a different Salesforce app must not disturb the rest."""
    from app.salesforce.apps import resolve_client

    db.add_all(
        [
            SalesforceApp(
                company_id=company.id, name="Company wide", client_id="company-key"
            ),
            SalesforceApp(
                company_id=company.id,
                project_id=project.id,
                name="This project",
                client_id="project-key",
            ),
        ]
    )
    await db.commit()

    client = await resolve_client(db, company_id=company.id, project_id=project.id)
    assert client.client_id == "project-key"
    assert client.source == "project"


async def test_the_deployment_app_is_the_fallback(db, company, project, monkeypatch):
    monkeypatch.setattr(settings, "salesforce_client_id", "deployment-key")
    from app.salesforce.apps import resolve_client

    client = await resolve_client(db, company_id=company.id, project_id=project.id)
    assert client.source == "deployment"
    assert client.app_id is None


async def test_a_deployment_can_require_customers_to_bring_their_own(
    db, company, project, monkeypatch
):
    """What a security team that will not authorise a third-party app asks for."""
    monkeypatch.setattr(settings, "salesforce_client_id", "deployment-key")
    monkeypatch.setattr(settings, "feature_require_customer_salesforce_app", True)
    from app.salesforce.apps import resolve_client
    from app.salesforce.errors import SalesforceAuthError

    with pytest.raises(SalesforceAuthError) as caught:
        await resolve_client(db, company_id=company.id, project_id=project.id)
    assert caught.value.error_type == "COMPANY_APP_REQUIRED"


async def test_with_no_app_anywhere_the_refusal_says_what_to_do(
    db, company, project, monkeypatch
):
    monkeypatch.setattr(settings, "salesforce_client_id", "")
    from app.salesforce.apps import resolve_client
    from app.salesforce.errors import SalesforceAuthError

    with pytest.raises(SalesforceAuthError) as caught:
        await resolve_client(db, company_id=company.id, project_id=project.id)
    # The callback URL is the thing people get wrong, so the refusal carries it.
    assert settings.salesforce_callback_url in caught.value.suggested_action


async def test_an_inactive_app_is_not_used(db, company, project, monkeypatch):
    monkeypatch.setattr(settings, "salesforce_client_id", "deployment-key")
    from app.salesforce.apps import resolve_client

    db.add(
        SalesforceApp(
            company_id=company.id, name="Retired", client_id=CONSUMER_KEY, is_active=False
        )
    )
    await db.commit()

    client = await resolve_client(db, company_id=company.id, project_id=project.id)
    assert client.source == "deployment"


# ---------------------------------------------------------------------------
# Which app authorised what
# ---------------------------------------------------------------------------
async def test_the_handshake_records_the_app_it_started_with(db, company, project, user):
    """The token exchange must present the same client_id the authorize URL did.

    Re-resolving at exchange time would let an administrator editing the app
    mid-login swap the credentials underneath an in-flight handshake, which
    fails as an `invalid_grant` pointing nowhere near the cause.
    """
    from app.models import OAuthState
    from app.salesforce.oauth import build_authorize_url

    app_row = SalesforceApp(
        company_id=company.id, name="Theirs", client_id=CONSUMER_KEY, is_default=True
    )
    db.add(app_row)
    await db.commit()

    url = await build_authorize_url(
        db, user.id, company_id=company.id, project_id=project.id
    )
    await db.commit()

    assert f"client_id={CONSUMER_KEY}" in url
    state = (await db.execute(select(OAuthState))).scalars().one()
    assert state.salesforce_app_id == app_row.id


async def test_the_authorize_url_carries_the_derived_callback(db, company, project, user):
    """Not a hand-typed string that may not match a mounted route."""
    from app.salesforce.oauth import build_authorize_url

    db.add(SalesforceApp(company_id=company.id, name="Theirs", client_id=CONSUMER_KEY))
    await db.commit()

    url = await build_authorize_url(
        db, user.id, company_id=company.id, project_id=project.id
    )
    assert settings.salesforce_callback_path in url.replace("%2F", "/")


async def test_removing_an_app_reports_the_connections_it_breaks(
    client, auth, db, connection
):
    """Those tokens can no longer be refreshed. Saying so beats discovering it
    mid-run as an authentication failure."""
    created = (
        await client.post(f"{API}/salesforce/apps", headers=auth, json=_payload())
    ).json()
    connection.salesforce_app_id = created["id"]
    await db.commit()

    resp = await client.delete(f"{API}/salesforce/apps/{created['id']}", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["connections_affected"] == 1


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------
async def test_another_companys_app_is_invisible(db, company, project, monkeypatch):
    monkeypatch.setattr(settings, "salesforce_client_id", "deployment-key")
    from app.salesforce.apps import resolve_client

    rival = Company(name="Rival Corp", slug="rival-corp-sfapp")
    db.add(rival)
    await db.flush()
    db.add(
        SalesforceApp(company_id=rival.id, name="Rival app", client_id="rival-key")
    )
    await db.commit()

    client = await resolve_client(db, company_id=company.id, project_id=project.id)
    assert client.client_id == "deployment-key"


async def test_an_app_cannot_be_scoped_to_another_companys_project(client, auth, db):
    from app.models import Project

    rival = Company(name="Rival Two", slug="rival-two-sfapp")
    db.add(rival)
    await db.flush()
    theirs = Project(company_id=rival.id, name="Theirs", slug="theirs-sfapp")
    db.add(theirs)
    await db.commit()

    resp = await client.post(
        f"{API}/salesforce/apps", headers=auth, json=_payload(project_id=theirs.id)
    )
    assert resp.status_code == 404


async def test_registering_an_app_is_a_company_admin_action(client, db, project, company):
    """Connecting an org is a project action. Choosing which Salesforce app the
    whole company authenticates through is not."""
    member = User(email="dev@acme-sfapp.example.com", display_name="Dev")
    db.add(member)
    await db.flush()
    db.add(
        CompanyMembership(
            company_id=company.id, user_id=member.id, role=CompanyRole.COMPANY_MEMBER
        )
    )
    from app.models import ProjectMembership, ProjectRole

    db.add(
        ProjectMembership(
            company_id=company.id,
            project_id=project.id,
            user_id=member.id,
            role=ProjectRole.DEVELOPER,
        )
    )
    await db.commit()

    resp = await client.post(
        f"{API}/salesforce/apps",
        headers={
            "Authorization": f"Bearer {create_session_token(member.id, project.id)}",
            "X-Project-Id": project.id,
        },
        json=_payload(),
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The callback URL
# ---------------------------------------------------------------------------
def test_a_callback_missing_the_version_segment_is_detected(monkeypatch):
    """The exact misconfiguration that shipped: `/api/salesforce/oauth/callback`
    instead of `/api/v1/salesforce/oauth/callback`. Salesforce completes the
    login and redirects to a route that does not exist."""
    monkeypatch.setattr(
        settings,
        "salesforce_redirect_uri",
        "http://localhost:8000/api/salesforce/oauth/callback",
    )
    problem = settings.salesforce_redirect_uri_problem
    assert problem
    assert "/api/v1/salesforce/oauth/callback" in problem


def test_a_correct_callback_reports_no_problem(monkeypatch):
    monkeypatch.setattr(
        settings,
        "salesforce_redirect_uri",
        "https://agent.example.com/api/v1/salesforce/oauth/callback",
    )
    assert settings.salesforce_redirect_uri_problem == ""


def test_the_callback_is_derived_when_it_is_not_overridden(monkeypatch):
    monkeypatch.setattr(settings, "salesforce_redirect_uri", "")
    monkeypatch.setattr(settings, "public_base_url", "https://agent.example.com")
    assert (
        settings.salesforce_callback_url
        == "https://agent.example.com/api/v1/salesforce/oauth/callback"
    )
    assert settings.salesforce_redirect_uri_problem == ""


async def test_a_misconfigured_callback_is_refused_before_the_round_trip(
    db, company, project, user, monkeypatch
):
    """Refusing here costs a second. Refusing after the redirect costs a
    complete Salesforce login and reads as Salesforce's fault."""
    monkeypatch.setattr(
        settings,
        "salesforce_redirect_uri",
        "http://localhost:8000/api/salesforce/oauth/callback",
    )
    monkeypatch.setattr(settings, "salesforce_client_id", "deployment-key")
    from app.salesforce.errors import SalesforceAuthError
    from app.salesforce.oauth import build_authorize_url

    with pytest.raises(SalesforceAuthError) as caught:
        await build_authorize_url(
            db, user.id, company_id=company.id, project_id=project.id
        )
    assert caught.value.error_type == "CALLBACK_MISCONFIGURED"


async def test_the_config_endpoint_hands_over_the_exact_callback_to_register(
    client, auth
):
    """The string an administrator pastes into Salesforce. Generated from the
    route that is actually mounted, so the two cannot disagree."""
    body = (await client.get(f"{API}/salesforce/config", headers=auth)).json()
    assert body["callback_url"].endswith("/api/v1/salesforce/oauth/callback")


async def test_the_app_list_carries_the_setup_instructions(client, auth):
    body = (await client.get(f"{API}/salesforce/apps", headers=auth)).json()
    assert body["callback_url"].endswith("/api/v1/salesforce/oauth/callback")
    assert "External Client App" in body["instructions"]
    assert "refresh_token" in " ".join(body["required_scopes"])
