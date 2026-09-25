"""Salesforce org connection endpoints (OAuth 2.0 web-server flow).

A Salesforce connection belongs to a *project*, and the project is the
isolation key on every lookup here. Tokens are never returned by any of these
endpoints — only a non-reversible fingerprint, so an operator can confirm which
credential is in use without ever seeing it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.config import settings
from app.models import (
    AuditEvent,
    Environment,
    Project,
    ProjectRole,
    SalesforceApp,
    SalesforceConnection,
)
from app.observability.logging import get_logger
from app.salesforce.apps import client_for_app_id, find_app
from app.salesforce.client import SalesforceClient
from app.salesforce.errors import SalesforceError
from app.salesforce.oauth import (
    build_authorize_url,
    consume_state,
    exchange_code,
    fetch_identity,
    upsert_connection,
)
from app.security.auth import DbSession, Tenant, decode_session_token
from app.security.secrets import SecretContext, fingerprint, store_secret
from app.tenancy import service as tenancy

router = APIRouter(prefix="/salesforce", tags=["salesforce"])
log = get_logger("api.salesforce")


class ConnectionOut(BaseModel):
    id: str
    label: str
    sf_org_id: str
    username: str
    instance_url: str
    org_type: str
    environment: str
    is_sandbox: bool
    api_version: str
    is_active: bool
    #: Non-reversible fingerprint of the access token. Confirms *which* token is
    #: stored without revealing any part of it.
    token_fingerprint: str = ""
    has_refresh_token: bool = False
    last_validated_at: str | None = None
    last_error: str | None = None

    @classmethod
    def of(cls, c: SalesforceConnection) -> ConnectionOut:
        return cls(
            id=c.id,
            label=c.label,
            sf_org_id=c.sf_org_id,
            username=c.username,
            instance_url=c.instance_url,
            org_type=c.org_type,
            environment=c.environment.value,
            is_sandbox=c.is_sandbox,
            api_version=c.api_version,
            is_active=c.is_active,
            token_fingerprint=c.token_fingerprint,
            has_refresh_token=bool(c.refresh_token_ref),
            last_validated_at=c.last_validated_at.isoformat() if c.last_validated_at else None,
            last_error=c.last_error,
        )


@router.get("/config")
async def config(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """What this project would connect an org with, and what is missing.

    Tenant-scoped rather than global, because "is Salesforce configured" is now
    a per-company question: a company with its own External Client App can
    connect even when the deployment has no shared app at all.
    """
    found = await find_app(db, company_id=tenant.company_id, project_id=tenant.project_id)
    company_app, source = (found if found else (None, ""))
    problem = settings.salesforce_redirect_uri_problem

    return {
        # True when *this project* can start an OAuth flow.
        "configured": bool(company_app or settings.salesforce_client_id) and not problem,
        "app": (
            {
                "id": company_app.id,
                "name": company_app.name,
                "client_id": company_app.client_id,
                "has_client_secret": bool(company_app.client_secret_ref),
                "login_url": company_app.login_url,
                "scope": source,
            }
            if company_app
            else None
        ),
        "using_deployment_app": company_app is None and bool(settings.salesforce_client_id),
        "customer_apps_enabled": settings.feature_customer_salesforce_apps,
        "customer_app_required": settings.feature_require_customer_salesforce_app,
        "login_url": settings.salesforce_login_url,
        # The exact string to paste into the External Client App. Derived from
        # the route that is actually mounted, so it cannot disagree with it.
        "callback_url": settings.salesforce_callback_url,
        "callback_problem": problem or None,
        "api_version": settings.salesforce_api_version,
        "scopes": settings.sf_scope_list,
        "allow_production_mutations": settings.allow_production_mutations,
        "environments": [e.value for e in Environment],
        "api_version_note": "Set SALESFORCE_API_VERSION to target a different release.",
        "setup_hint": _setup_hint(company_app, problem),
    }


def _setup_hint(company_app: Any, problem: str) -> str | None:
    if problem:
        return problem
    if company_app is not None:
        return None
    if settings.feature_require_customer_salesforce_app:
        return (
            "This deployment requires your company to register its own Salesforce "
            "External Client App before an org can be connected."
        )
    if settings.salesforce_client_id:
        return None
    return (
        "No Salesforce app is available yet. Register an External Client App in "
        "Salesforce Setup, then add its consumer key and secret under Settings — "
        f"the callback URL to register is {settings.salesforce_callback_url}"
    )


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(tenant: Tenant, db: DbSession) -> list[ConnectionOut]:
    rows = (
        (
            await db.execute(
                select(SalesforceConnection)
                .where(SalesforceConnection.project_id == tenant.project_id)
                .order_by(SalesforceConnection.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [ConnectionOut.of(c) for c in rows]


@router.post("/oauth/start")
async def oauth_start(
    tenant: Tenant,
    db: DbSession,
    sandbox: bool = Query(False, description="Use test.salesforce.com"),
    login_url: str | None = Query(None, description="Custom My Domain login URL"),
    environment: Environment | None = Query(
        None, description="Which environment this org represents"
    ),
    label: str = Query("", description="Human-readable name for this connection"),
) -> dict[str, str]:
    # Connecting a Salesforce org gives the whole project reach into it.
    tenant.require(
        ProjectRole.PROJECT_ADMIN,
        ProjectRole.SALESFORCE_ADMIN,
        ProjectRole.RELEASE_MANAGER,
        ProjectRole.DEVELOPER,
    )
    if environment is not None and not tenant.policy.environment_allowed(environment):
        raise HTTPException(
            status_code=403,
            detail=(
                f"This project's policy does not permit connecting a "
                f"{environment.value} org."
            ),
        )
    try:
        url = await build_authorize_url(
            db,
            tenant.user_id,
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            sandbox=sandbox,
            login_url=login_url,
            redirect_after=_pack_intent(environment, label),
        )
    except SalesforceError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    await db.commit()
    return {"authorize_url": url}


def _settle_environment(
    conn: SalesforceConnection, *, declared: Environment | None
) -> None:
    """Correct an auto-derived PRODUCTION label once the org type is known.

    `upsert_connection` runs before anything has asked Salesforce what kind of
    org this is, so it derives the environment from the only signal it has: the
    sandbox flag. A **Developer Edition** org is not a sandbox, so it comes out
    labelled PRODUCTION.

    That single line made a carve-out in the risk engine unreachable. The engine
    knows a Developer Edition org "is not a sandbox and is also nobody's
    production" — but it checks the declared environment first, and a
    declaration may only ever *raise* the posture. Having auto-declared
    PRODUCTION, nothing downstream could ever reach the carve-out. Every
    Developer Edition org was therefore treated as production, needing
    production clearance to create a field in a scratch org nobody depends on.

    Only an auto-derived label is corrected. An operator who explicitly said
    PRODUCTION means it, and their declaration still stands.
    """
    if declared is not None:
        return
    if "developer" not in (conn.org_type or "").lower():
        return
    if conn.environment is not Environment.PRODUCTION:
        return
    conn.environment = Environment.DEVELOPMENT
    if not conn.label or conn.label == Environment.PRODUCTION.value.title():
        conn.label = conn.username or "Developer Edition"
    log.info(
        "salesforce.environment_corrected",
        connection_id=conn.id,
        org_type=conn.org_type,
        to=conn.environment.value,
    )


def _pack_intent(environment: Environment | None, label: str) -> str:
    """Carry the operator's environment/label choice across the OAuth round-trip.

    It rides in the server-side state row, never in the redirect URL, so a user
    cannot alter which environment their org is recorded as by editing a link.
    """
    return f"{environment.value if environment else ''}|{label[:120]}"


def _unpack_intent(raw: str) -> tuple[Environment | None, str]:
    env_value, _, label = (raw or "").partition("|")
    try:
        environment = Environment(env_value) if env_value else None
    except ValueError:
        environment = None
    return environment, label


@router.get("/oauth/callback")
async def oauth_callback(
    db: DbSession,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    front = settings.frontend_base_url.rstrip("/")
    if error:
        log.warning("salesforce.oauth_denied", error=error)
        return RedirectResponse(f"{front}/connections?error={error}")
    if not code or not state:
        return RedirectResponse(f"{front}/connections?error=missing_code")

    try:
        state_row = await consume_state(db, state)
        environment, label = _unpack_intent(state_row.redirect_after)
        # The same app that built the authorize URL, re-resolved from the state
        # row rather than from current configuration.
        oauth_client = await client_for_app_id(
            db, state_row.salesforce_app_id, company_id=state_row.company_id
        )
        bundle = await exchange_code(
            state_row.login_url, code, state_row.code_verifier, oauth_client
        )
        identity = await fetch_identity(bundle)
        conn = await upsert_connection(
            db,
            state_row.user_id,
            bundle,
            identity,
            state_row.login_url,
            company_id=state_row.company_id,
            project_id=state_row.project_id,
            environment=environment,
            label=label,
            salesforce_app_id=state_row.salesforce_app_id,
        )
        async with SalesforceClient(conn, db) as sf:
            await sf.validate()
        _settle_environment(conn, declared=environment)
        await db.commit()
    except SalesforceError as exc:
        await db.rollback()
        log.error("salesforce.oauth_failed", error=exc.message)
        return RedirectResponse(f"{front}/connections?error={exc.error_type}")

    return RedirectResponse(f"{front}/connections?connected={conn.id}")


# ---------------------------------------------------------------------------
# A company's own Salesforce External Client App
#
# The consumer secret arrives here once and goes straight to the secret store.
# No endpoint returns it. A caller can see that a secret exists and a
# fingerprint of which one — enough to confirm a rotation took effect, and not
# enough to use.
# ---------------------------------------------------------------------------
class SalesforceAppIn(BaseModel):
    name: str = Field("Salesforce app", max_length=120)
    client_id: str = Field(..., min_length=10, max_length=512)
    #: Write-only. There is no endpoint that reads it back.
    client_secret: str | None = None
    login_url: str = "https://login.salesforce.com"
    api_version: str = ""
    #: Null scopes the app to the whole company. Naming a project restricts it
    #: to that project, which is how one team points at a different app.
    project_id: str | None = None
    is_default: bool = True


def _app_out(app: SalesforceApp) -> dict[str, Any]:
    return {
        "id": app.id,
        "name": app.name,
        "client_id": app.client_id,
        "has_client_secret": bool(app.client_secret_ref),
        "client_secret_fingerprint": (
            fingerprint(app.client_secret_ref)[:12] if app.client_secret_ref else ""
        ),
        "login_url": app.login_url,
        "api_version": app.api_version or settings.salesforce_api_version,
        "project_id": app.project_id,
        "scope": "project" if app.project_id else "company",
        "is_default": app.is_default,
        "is_active": app.is_active,
        "updated_at": app.updated_at.isoformat() if app.updated_at else None,
    }


@router.get("/apps")
async def list_salesforce_apps(tenant: Tenant, db: DbSession) -> dict[str, Any]:
    """Every Salesforce app this company has registered."""
    tenant.require_company_admin()
    rows = (
        await db.execute(
            select(SalesforceApp).where(SalesforceApp.company_id == tenant.company_id)
        )
    ).scalars().all()
    return {
        "apps": [_app_out(a) for a in rows],
        "enabled": settings.feature_customer_salesforce_apps,
        "required": settings.feature_require_customer_salesforce_app,
        "deployment_app_available": bool(settings.salesforce_client_id),
        # Everything an administrator needs to register the app on the
        # Salesforce side, in the exact form Salesforce wants it.
        "callback_url": settings.salesforce_callback_url,
        "required_scopes": settings.sf_scope_list,
        "instructions": (
            "Salesforce Setup -> External Client App Manager -> New External Client "
            "App. Enable OAuth, paste the callback URL above, select the scopes "
            "above, enable the Authorization Code flow and require PKCE. Then copy "
            "the consumer key and secret here. New Connected Apps cannot be created "
            "by default since Spring '26; existing ones keep working."
        ),
    }


@router.post("/apps", status_code=201)
async def create_salesforce_app(
    payload: SalesforceAppIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Register this company's own External Client App.

    Company administrators only: this decides which Salesforce app every
    project in the company authenticates through, which is a higher authority
    than connecting an org.
    """
    tenant.require_company_admin()
    if not settings.feature_customer_salesforce_apps:
        raise HTTPException(
            status_code=400,
            detail=(
                "Customer-owned Salesforce apps are disabled in this deployment. "
                "Connections use the deployment-managed app."
            ),
        )
    if payload.project_id:
        await _require_own_project(db, tenant, payload.project_id)

    existing = (
        await db.execute(
            select(SalesforceApp).where(
                SalesforceApp.company_id == tenant.company_id,
                SalesforceApp.name == payload.name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409, detail=f"An app named '{payload.name}' already exists."
        )

    app = SalesforceApp(
        company_id=tenant.company_id,
        project_id=payload.project_id,
        name=payload.name,
        client_id=payload.client_id.strip(),
        login_url=payload.login_url.rstrip("/") or "https://login.salesforce.com",
        api_version=payload.api_version.strip(),
        is_default=payload.is_default,
        created_by=tenant.user_id,
    )
    if payload.client_secret:
        app.client_secret_ref = store_secret(
            payload.client_secret,
            SecretContext(
                company_id=tenant.company_id,
                project_id=payload.project_id or "",
                purpose="salesforce_client_secret",
            ),
        )
    db.add(app)
    await db.flush()
    if payload.is_default:
        await _clear_other_defaults(db, tenant.company_id, app)

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=payload.project_id,
            user_id=tenant.user_id,
            action="salesforce.app_registered",
            arguments={
                "app_id": app.id,
                "name": app.name,
                # The consumer key, never the secret — it travels in every
                # authorize URL anyway, and it is what makes a mismatch
                # diagnosable from the audit trail.
                "client_id": app.client_id,
                "scope": "project" if app.project_id else "company",
                "secret_provided": bool(payload.client_secret),
            },
            outcome="ok",
        )
    )
    await db.commit()
    return _app_out(app)


@router.patch("/apps/{app_id}")
async def update_salesforce_app(
    app_id: str, payload: SalesforceAppIn, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Update an app, or rotate its consumer secret.

    Existing connections keep working: each records the app it was authorised
    with, and a rotated secret belongs to the same app.
    """
    tenant.require_company_admin()
    app = await db.get(SalesforceApp, app_id)
    if app is None or app.company_id != tenant.company_id:
        raise HTTPException(status_code=404, detail="Salesforce app not found.")
    if payload.project_id:
        await _require_own_project(db, tenant, payload.project_id)

    app.name = payload.name
    app.client_id = payload.client_id.strip()
    app.login_url = payload.login_url.rstrip("/") or "https://login.salesforce.com"
    app.api_version = payload.api_version.strip()
    app.project_id = payload.project_id
    app.is_default = payload.is_default
    if payload.client_secret:
        app.client_secret_ref = store_secret(
            payload.client_secret,
            SecretContext(
                company_id=tenant.company_id,
                project_id=payload.project_id or "",
                purpose="salesforce_client_secret",
            ),
        )
    if payload.is_default:
        await _clear_other_defaults(db, tenant.company_id, app)

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=app.project_id,
            user_id=tenant.user_id,
            action="salesforce.app_updated",
            arguments={
                "app_id": app.id,
                "name": app.name,
                "client_id": app.client_id,
                "secret_rotated": bool(payload.client_secret),
            },
            outcome="ok",
        )
    )
    await db.commit()
    return _app_out(app)


@router.delete("/apps/{app_id}")
async def delete_salesforce_app(
    app_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    """Remove an app, and say what it will cost.

    Connections authorised through it can no longer refresh their tokens, so
    the count of affected connections is returned rather than discovered later
    as an authentication failure mid-run.
    """
    tenant.require_company_admin()
    app = await db.get(SalesforceApp, app_id)
    if app is None or app.company_id != tenant.company_id:
        raise HTTPException(status_code=404, detail="Salesforce app not found.")

    affected = (
        await db.execute(
            select(SalesforceConnection).where(
                SalesforceConnection.company_id == tenant.company_id,
                SalesforceConnection.salesforce_app_id == app.id,
                SalesforceConnection.is_active.is_(True),
            )
        )
    ).scalars().all()

    await db.delete(app)
    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            user_id=tenant.user_id,
            action="salesforce.app_removed",
            arguments={"app_id": app_id, "connections_affected": len(affected)},
            outcome="ok",
        )
    )
    await db.commit()
    return {
        "success": True,
        "connections_affected": len(affected),
        "message": (
            f"{len(affected)} connection(s) were authorised through this app and can "
            "no longer refresh their tokens. Reconnect those orgs."
            if affected
            else "No connections were using this app."
        ),
    }


async def _clear_other_defaults(
    db: Any, company_id: str, keep: SalesforceApp
) -> None:
    """Exactly one default per scope, so resolution is never ambiguous."""
    rows = (
        await db.execute(
            select(SalesforceApp).where(
                SalesforceApp.company_id == company_id,
                SalesforceApp.id != keep.id,
            )
        )
    ).scalars().all()
    for row in rows:
        if row.project_id == keep.project_id:
            row.is_default = False


async def _require_own_project(db: Any, tenant: Tenant, project_id: str) -> None:
    """An app scoped to another company's project would be a tenancy breach."""
    project = await db.get(Project, project_id)
    if project is None or project.company_id != tenant.company_id:
        raise HTTPException(
            status_code=404, detail=f"Project '{project_id}' not found in this company."
        )


class ConnectionUpdate(BaseModel):
    label: str | None = None
    environment: Environment | None = None


@router.patch("/connections/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    connection_id: str, payload: ConnectionUpdate, tenant: Tenant, db: DbSession
) -> ConnectionOut:
    """Rename a connection or correct which environment it represents.

    Environment drives policy, so changing it is an administrator action.
    """
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.SALESFORCE_ADMIN)
    conn = await _owned(db, tenant.project_id, connection_id)
    if payload.label is not None:
        conn.label = payload.label[:120]
    if payload.environment is not None:
        if not tenant.policy.environment_allowed(payload.environment):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"This project's policy does not permit the "
                    f"{payload.environment.value} environment."
                ),
            )
        conn.environment = payload.environment
    await db.commit()
    return ConnectionOut.of(conn)


@router.post("/connections/{connection_id}/validate")
async def validate_connection(
    connection_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    conn = await _owned(db, tenant.project_id, connection_id)
    try:
        async with SalesforceClient(conn, db) as sf:
            info = await sf.validate()
        conn.last_error = None
        await db.commit()
    except SalesforceError as exc:
        conn.is_active = False
        conn.last_error = exc.message
        await db.commit()
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    return {"success": True, "connection": ConnectionOut.of(conn).model_dump(), "org": info}


@router.delete("/connections/{connection_id}")
async def disconnect(connection_id: str, tenant: Tenant, db: DbSession) -> dict[str, bool]:
    tenant.require(ProjectRole.PROJECT_ADMIN, ProjectRole.SALESFORCE_ADMIN)
    conn = await _owned(db, tenant.project_id, connection_id)
    await db.delete(conn)
    await db.commit()
    return {"success": True}


async def _owned(db: Any, project_id: str, connection_id: str) -> SalesforceConnection:
    conn = await tenancy.owned(db, SalesforceConnection, connection_id, project_id)
    if conn is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return conn


async def resolve_connection(
    db: Any, project_id: str, connection_id: str | None
) -> SalesforceConnection | None:
    """Project-isolated lookup used by the agent routes.

    Isolation is on `project_id`, not on who happened to click Connect: a
    connection belongs to the project, and a member of another project can
    never reach it even with a valid-looking id.
    """
    if not connection_id:
        return None
    conn = await tenancy.owned(db, SalesforceConnection, connection_id, project_id)
    if conn is None or not conn.is_active:
        return None
    return conn


__all__ = ["ConnectionOut", "decode_session_token", "resolve_connection", "router"]
