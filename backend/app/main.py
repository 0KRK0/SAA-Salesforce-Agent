"""FastAPI application entrypoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    routes_agent,
    routes_ai,
    routes_approvals,
    routes_audit,
    routes_auth,
    routes_conversations,
    routes_integrations,
    routes_operations,
    routes_project,
    routes_salesforce,
    routes_scim,
    routes_sso,
    routes_traceability,
)
from app.config import settings
from app.db import SessionLocal, init_db
from app.execution.worker import RunWorker
from app.observability.logging import configure_logging, get_logger, request_id_var
from app.salesforce.errors import SalesforceError
from app.security.ratelimit import describe as describe_rate_limits
from app.security.ratelimit import keys_on_session, limit_for, limiter
from app.security.secrets import SecretError, describe_backend
from app.tools.base import ToolValidationError
from app.tools.registry import build_registry

configure_logging(settings.log_level)
log = get_logger("app")

#: The value app.config ships as a default. Running production on it would mean
#: every session token is forgeable, so startup says so loudly.
_DEV_SESSION_SECRET = "dev-only-insecure-session-secret-change-me"  # noqa: S105


#: The in-process run worker. Every API process runs one by default; claims are
#: atomic, so several processes can share a queue without coordination.
worker: RunWorker | None = None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global worker
    await init_db()
    registry = build_registry()
    log.info(
        "app.startup",
        environment=settings.environment,
        tools=registry.names(),
        deployment_llm_providers=settings.deployment_llm_providers,
        platform_managed_ai=settings.feature_platform_managed_ai,
        salesforce_configured=settings.salesforce_configured,

        mcp_enabled=settings.mcp_enabled,
        oidc_configured=settings.oidc_configured,
        scim_configured=settings.scim_configured,
        secret_backend=describe_backend(),
        features=settings.feature_flags,
        api_prefix=settings.api_v1,
        run_worker_enabled=settings.run_worker_enabled,
    )
    # A callback URL that does not match a mounted route fails at the very end
    # of a *successful* Salesforce login: the person authenticates, consents,
    # and lands on a 404 holding a valid authorization code. It reads as
    # Salesforce's fault and it is entirely ours, so it is said here, loudly, at
    # startup — long before anyone spends a round-trip discovering it.
    if settings.salesforce_redirect_uri_problem:
        log.error(
            "app.salesforce_callback_misconfigured",
            detail=settings.salesforce_redirect_uri_problem,
        )
    else:
        log.info("app.salesforce_callback", url=settings.salesforce_callback_url)

    if settings.environment == "production":
        for message, ok in (
            ("No secret backend is configured — credentials cannot be stored at "
             "rest. Set ENCRYPTION_KEY, or SECRET_BACKEND with its key id.",
             settings.secrets_configured),
            ("SESSION_SECRET is still the development default.",
             settings.session_secret != _DEV_SESSION_SECRET),
        ):
            if not ok:
                log.error("app.insecure_configuration", detail=message)
    if settings.run_worker_enabled:
        worker = RunWorker(SessionLocal)
        worker.start()
    else:
        log.warning(
            "app.worker_disabled",
            detail=(
                "RUN_WORKER_ENABLED is false. Queued runs will not execute in "
                "this process; a separate worker must claim them."
            ),
        )

    yield

    if worker is not None:
        # In-flight runs are allowed to finish. A run interrupted between a
        # Salesforce write and its verification is the one state this system
        # cannot describe honestly.
        await worker.stop()
        worker = None
    log.info("app.shutdown")


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next: Any) -> Any:
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
    token = request_id_var.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def rate_limit(request: Request, call_next: Any) -> Any:
    """Coarse per-process limiting on the routes where it earns its keep.

    Keyed on the session when there is one, and on the client address when
    there is not — otherwise a single shared NAT would rate-limit a whole
    office out of logging in.
    """
    if not settings.rate_limit_enabled or request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    name, limit = limit_for(path)
    decision = limiter.check(
        f"{name}:{_rate_key(request, session_keyed=keys_on_session(path))}", limit
    )
    if not decision.allowed:
        return JSONResponse(
            status_code=429,
            content={
                "success": False,
                "error_type": "RATE_LIMITED",
                "message": (
                    "Too many requests. Wait a moment and try again."
                ),
                "retryable": True,
                "retry_after_seconds": decision.retry_after,
            },
            headers=decision.headers(),
        )

    response = await call_next(request)
    for key, value in decision.headers().items():
        response.headers[key] = value
    return response


def _rate_key(request: Request, *, session_keyed: bool = True) -> str:
    """Who this request counts against.

    Authenticated routes key on the session, which is fairer: one person's
    runaway script should not rate-limit their colleague behind the same NAT.

    Unauthenticated routes — login, invitation redemption, SCIM — key on the
    client address only. Keying those on a cookie the caller chose would let an
    attacker reset their own limit by rotating a junk value, making the limiter
    evadable by exactly the traffic it exists to stop.

    Either way the value is hashed. Limiter keys end up in memory dumps and
    logs, and a raw session token there would be a credential in a place
    nothing else in the system is guarding.
    """
    import hashlib

    if session_keyed:
        session = request.cookies.get("sfagent_session") or request.headers.get(
            "authorization", ""
        )
        if session:
            return "s:" + hashlib.sha256(session.encode()).hexdigest()[:24]
    client = request.client.host if request.client else "unknown"
    return "ip:" + hashlib.sha256(client.encode()).hexdigest()[:24]


@app.exception_handler(SalesforceError)
async def salesforce_error_handler(_: Request, exc: SalesforceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code or 400, content=exc.to_dict())


@app.exception_handler(SecretError)
async def secret_error_handler(_: Request, exc: SecretError) -> JSONResponse:
    """A secret-store failure must never leak what it was handling.

    The operator gets the detail in the logs; the caller gets a stable code and
    an instruction, because the alternative is a stack trace with a key in it.
    """
    log.error("secret.error", detail=str(exc))
    return JSONResponse(
        status_code=503,
        content={
            "success": False,
            "error_type": "SECRET_STORE_UNAVAILABLE",
            "message": (
                "A stored credential could not be read. No action was taken."
            ),
            "suggested_action": (
                "Check the deployment's SECRET_BACKEND configuration, or re-enter "
                "the credential."
            ),
        },
    )


@app.exception_handler(ToolValidationError)
async def tool_validation_handler(_: Request, exc: ToolValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content=exc.to_dict())


@app.get("/health")
async def health() -> dict[str, Any]:
    registry = build_registry()
    return {
        "status": "ok",
        "environment": settings.environment,
        # The platform is model-independent: what matters is whether a project
        # can reach *a* model, not whether one particular vendor is configured.
        "deployment_llm_providers": settings.deployment_llm_providers,
        "platform_managed_ai": settings.feature_platform_managed_ai,
        "ai_available_without_byok": settings.ai_available_without_byok,
        # Whether a *shared* deployment app exists. A company that registered
        # its own can connect an org regardless, so this is not the answer to
        # "is Salesforce available here".
        "salesforce_configured": settings.salesforce_configured,
        "salesforce_callback_url": settings.salesforce_callback_url,
        "salesforce_callback_problem": settings.salesforce_redirect_uri_problem or None,
        "secrets_configured": settings.secrets_configured,
        "secret_backend": describe_backend(),
        "features": settings.feature_flags,
        "api_prefix": settings.api_v1,
        "run_worker_enabled": settings.run_worker_enabled,
        "rate_limiting": describe_rate_limits(),

        "max_agent_steps": settings.max_agent_steps,
        "mcp_enabled": settings.mcp_enabled,
        "oidc_configured": settings.oidc_configured,
        "scim_configured": settings.scim_configured,
        "allow_production_mutations": settings.allow_production_mutations,
        "tools": registry.names(),
    }


for router in (
    routes_auth.router,
    routes_project.router,
    routes_ai.router,
    routes_integrations.router,
    routes_sso.router,
    routes_scim.router,
    routes_salesforce.router,
    routes_conversations.router,
    routes_agent.router,
    routes_approvals.router,
    routes_audit.router,
    routes_traceability.router,
    routes_operations.router,
):
    app.include_router(router, prefix=settings.api_v1)
