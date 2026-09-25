"""Which Salesforce app a given project authenticates with.

A Connected App — an *External Client App* since Spring '26 — is the object a
Salesforce administrator uses to control who may reach their org through an
integration: permitted profiles, IP ranges, refresh-token lifetime, and a
single revoke button that ends every session at once.

Putting one shared app in the deployment's environment makes all of that ours
rather than the customer's, and couples every tenant to a single revocation.
So there are two sources, resolved in this order:

  1. **A project's own app** — one project pointing at a different Salesforce
     app than the rest of the company.
  2. **The company's app** — the normal enterprise case: their security team
     registered it, they own it, they can pull it.
  3. **The deployment's app** — the shared fallback from the environment, for
     self-serve signups that have no Salesforce administrator yet. It can be
     switched off, and a deployment that requires customers to bring their own
     sets FEATURE_REQUIRE_CUSTOMER_SALESFORCE_APP.

Nothing here reads a secret except at the moment of a token request, and the
resolved secret is never returned to a caller, logged, or placed in a response.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SalesforceApp
from app.salesforce.errors import SalesforceAuthError
from app.security.secrets import SecretContext, resolve_secret


@dataclass(frozen=True)
class OAuthClient:
    """The credentials one OAuth handshake will use, resolved once.

    `client_secret` is held only for the lifetime of a token request. It is
    excluded from `repr` so it cannot reach a log line through an exception
    message or a debugger dump.
    """

    client_id: str
    redirect_uri: str
    login_url: str
    api_version: str
    #: None when the deployment-managed app was used.
    app_id: str | None
    #: "project" | "company" | "deployment"
    source: str
    name: str = ""
    client_secret: str = field(default="", repr=False, compare=False)

    def describe(self) -> dict[str, str | None]:
        """Safe to return to a caller. Carries no secret."""
        return {
            "app_id": self.app_id,
            "name": self.name,
            "source": self.source,
            "client_id": self.client_id,
            "login_url": self.login_url,
            "redirect_uri": self.redirect_uri,
        }


def _secret_context(app: SalesforceApp) -> SecretContext:
    return SecretContext(
        company_id=app.company_id,
        project_id=app.project_id or "",
        purpose="salesforce_client_secret",
    )


def _from_row(app: SalesforceApp, source: str) -> OAuthClient:
    secret = ""
    if app.client_secret_ref:
        secret = resolve_secret(app.client_secret_ref, _secret_context(app))
    return OAuthClient(
        client_id=app.client_id,
        client_secret=secret,
        redirect_uri=settings.salesforce_callback_url,
        login_url=app.login_url or settings.salesforce_login_url,
        api_version=app.api_version or settings.salesforce_api_version,
        app_id=app.id,
        source=source,
        name=app.name,
    )


async def find_app(
    db: AsyncSession, *, company_id: str, project_id: str | None
) -> tuple[SalesforceApp, str] | None:
    """The most specific active app for this project, and where it came from."""
    if not settings.feature_customer_salesforce_apps:
        return None

    rows = (
        await db.execute(
            select(SalesforceApp).where(
                SalesforceApp.company_id == company_id,
                SalesforceApp.is_active.is_(True),
            )
        )
    ).scalars().all()

    usable = [r for r in rows if r.client_id]
    project_apps = [r for r in usable if r.project_id == project_id]
    if project_apps:
        return _preferred(project_apps), "project"
    company_apps = [r for r in usable if r.project_id is None]
    if company_apps:
        return _preferred(company_apps), "company"
    return None


def _preferred(apps: list[SalesforceApp]) -> SalesforceApp:
    """The one marked default, else the most recently updated."""
    for app in apps:
        if app.is_default:
            return app
    return sorted(apps, key=lambda a: a.updated_at or a.created_at)[-1]


async def resolve_client(
    db: AsyncSession, *, company_id: str, project_id: str | None
) -> OAuthClient:
    """The app this project will connect an org with, or a refusal that explains why."""
    problem = settings.salesforce_redirect_uri_problem
    if problem:
        # Refuse before the round-trip rather than after it. The alternative is
        # a person completing a Salesforce login and landing on a 404 holding a
        # valid authorization code, which reads as Salesforce's fault.
        raise SalesforceAuthError(
            error_type="CALLBACK_MISCONFIGURED",
            message=problem,
            suggested_action="Correct the callback URL and restart the backend.",
        )

    found = await find_app(db, company_id=company_id, project_id=project_id)
    if found is not None:
        app, source = found
        return _from_row(app, source)

    if settings.feature_require_customer_salesforce_app:
        raise SalesforceAuthError(
            error_type="COMPANY_APP_REQUIRED",
            message=(
                "This deployment requires each company to register its own "
                "Salesforce External Client App. No shared app will be used."
            ),
            suggested_action=(
                "Settings -> Salesforce app: register your External Client App's "
                "consumer key and secret, then connect the org."
            ),
        )

    if not settings.salesforce_client_id:
        raise SalesforceAuthError(
            error_type="CONFIG_MISSING",
            message=(
                "No Salesforce app is available: this company has not registered "
                "one and the deployment has no fallback configured."
            ),
            suggested_action=(
                "Register an External Client App in Salesforce, then add its "
                "consumer key and secret under Settings -> Salesforce app. The "
                "callback URL to register there is "
                f"{settings.salesforce_callback_url}"
            ),
        )

    return OAuthClient(
        client_id=settings.salesforce_client_id,
        client_secret=settings.salesforce_client_secret,
        redirect_uri=settings.salesforce_callback_url,
        login_url=settings.salesforce_login_url,
        api_version=settings.salesforce_api_version,
        app_id=None,
        source="deployment",
        name="Deployment-managed app",
    )


async def client_for_app_id(
    db: AsyncSession, app_id: str | None, *, company_id: str
) -> OAuthClient:
    """Re-resolve the exact app a handshake or connection was made with.

    A token exchange must present the same `client_id` the authorize URL did,
    and a refresh must be presented to the app that issued the token. Resolving
    afresh would let an administrator editing the company's app mid-flight swap
    the credentials underneath an in-flight exchange, which fails with an
    `invalid_grant` that points nowhere near the cause.
    """
    if app_id is None:
        return OAuthClient(
            client_id=settings.salesforce_client_id,
            client_secret=settings.salesforce_client_secret,
            redirect_uri=settings.salesforce_callback_url,
            login_url=settings.salesforce_login_url,
            api_version=settings.salesforce_api_version,
            app_id=None,
            source="deployment",
            name="Deployment-managed app",
        )

    app = await db.get(SalesforceApp, app_id)
    if app is None or app.company_id != company_id:
        raise SalesforceAuthError(
            error_type="APP_UNAVAILABLE",
            message=(
                "The Salesforce app this connection was authorised with no longer "
                "exists. Tokens issued by it cannot be refreshed."
            ),
            suggested_action=(
                "Re-register the app, or reconnect the org with the current one."
            ),
        )
    return _from_row(app, "project" if app.project_id else "company")


__all__ = ["OAuthClient", "client_for_app_id", "find_app", "resolve_client"]
