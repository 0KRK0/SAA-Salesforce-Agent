"""Test fixtures.

Environment is configured BEFORE app modules are imported so that
app.config.settings picks up the test values.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

os.environ.setdefault("ENVIRONMENT", "local")
os.environ.setdefault("ENCRYPTION_KEY", "1TR2H0mvvPnDcRnFYtRVWkxEJ6mFTb9zC7iFRZBWDzM=")
os.environ.setdefault("SESSION_SECRET", "test-session-secret")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("SALESFORCE_CLIENT_ID", "test-client-id")
os.environ.setdefault("SALESFORCE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("MAX_AGENT_STEPS", "6")
_DB_PATH = os.path.join(tempfile.gettempdir(), "sfagent_test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_DB_PATH}")

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import (  # noqa: E402
    Company,
    CompanyMembership,
    CompanyRole,
    Conversation,
    Environment,
    Project,
    ProjectMembership,
    ProjectPolicy,
    ProjectRole,
    SalesforceConnection,
    User,
)
from app.salesforce.client import SalesforceClient  # noqa: E402
from app.security.secrets import SecretContext, store_secret  # noqa: E402

pytest_plugins: list[str] = []


# --------------------------------------------------------------- fake Salesforce
ACCOUNT_DESCRIBE: dict[str, Any] = {
    "name": "Account",
    "label": "Account",
    "labelPlural": "Accounts",
    "custom": False,
    "keyPrefix": "001",
    "createable": True,
    "updateable": True,
    "deletable": True,
    "queryable": True,
    "fields": [
        {
            "name": "Id", "label": "Account ID", "type": "id", "nillable": False,
            "createable": False, "updateable": False, "custom": False,
        },
        {
            "name": "Name", "label": "Account Name", "type": "string", "length": 255,
            "nillable": False, "createable": True, "updateable": True, "custom": False,
            "defaultedOnCreate": False,
        },
        {
            "name": "Industry", "label": "Industry", "type": "picklist", "nillable": True,
            "createable": True, "updateable": True, "custom": False,
            "restrictedPicklist": True,
            "picklistValues": [
                {"value": "Technology", "label": "Technology", "active": True},
                {"value": "Banking", "label": "Banking", "active": True},
            ],
        },
        {
            "name": "CreatedDate", "label": "Created Date", "type": "datetime",
            "nillable": False, "createable": False, "updateable": False, "custom": False,
            "defaultedOnCreate": True,
        },
    ],
    "childRelationships": [
        {"childSObject": "Contact", "field": "AccountId", "relationshipName": "Contacts"}
    ],
}


class FakeSalesforce:
    """In-memory Salesforce REST double driven through httpx.MockTransport."""

    def __init__(self) -> None:
        self.describe_calls: list[str] = []
        self.created: list[tuple[str, dict]] = []
        self.updated: list[tuple[str, str, dict]] = []
        self.records: dict[str, dict] = {
            "001000000000001AAA": {"Id": "001000000000001AAA", "Name": "Acme Corporation"}
        }
        self.extra_fields: list[dict] = []
        self.fail_next_query: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/describe"):
            sobject = path.split("/sobjects/")[1].split("/")[0]
            self.describe_calls.append(sobject)
            if sobject != "Account":
                return httpx.Response(
                    404,
                    json=[{"errorCode": "NOT_FOUND", "message": f"{sobject} not found"}],
                )
            payload = {
                **ACCOUNT_DESCRIBE,
                "fields": ACCOUNT_DESCRIBE["fields"] + self.extra_fields,
            }
            return httpx.Response(200, json=payload)

        if path.endswith("/sobjects"):
            return httpx.Response(200, json={"sobjects": [{"name": "Account", "label": "Account"}]})

        if path.endswith("/query"):
            if self.fail_next_query:
                code, self.fail_next_query = self.fail_next_query, None
                return httpx.Response(400, json=[{"errorCode": code, "message": "bad query"}])
            return httpx.Response(
                200,
                json={
                    "totalSize": len(self.records),
                    "done": True,
                    "records": [
                        {"attributes": {"type": "Account"}, **r} for r in self.records.values()
                    ],
                },
            )

        if "/sobjects/" in path:
            parts = path.split("/sobjects/")[1].split("/")
            sobject = parts[0]
            if request.method == "POST":
                import json as _json

                body = _json.loads(request.content or b"{}")
                new_id = f"001000000000{len(self.records) + 2:03d}AAA"
                self.records[new_id] = {"Id": new_id, **body}
                self.created.append((sobject, body))
                return httpx.Response(201, json={"id": new_id, "success": True, "errors": []})
            record_id = parts[1] if len(parts) > 1 else ""
            if request.method == "PATCH":
                import json as _json

                body = _json.loads(request.content or b"{}")
                self.records.setdefault(record_id, {"Id": record_id}).update(body)
                self.updated.append((sobject, record_id, body))
                return httpx.Response(204)
            if request.method == "GET":
                record = self.records.get(record_id)
                if record is None:
                    return httpx.Response(
                        404, json=[{"errorCode": "NOT_FOUND", "message": "not found"}]
                    )
                return httpx.Response(200, json={"attributes": {"type": sobject}, **record})

        if path.endswith("/limits"):
            return httpx.Response(200, json={"DailyApiRequests": {"Max": 15000, "Remaining": 1}})

        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": path}])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake_sf() -> FakeSalesforce:
    return FakeSalesforce()


@pytest.fixture(autouse=True)
def _isolate_rate_limiter():
    """Give each test its own rate-limit budget.

    The limiter is process-global and keys unauthenticated routes on the client
    address, which every test shares. Without this, one test's logins would
    exhaust the next test's allowance. Deliberately a reset rather than
    disabling the limiter: the middleware stays in the path for every test, so
    a change that broke it would still be caught.
    """
    from app.security.ratelimit import limiter

    limiter.reset()
    yield
    limiter.reset()


@pytest_asyncio.fixture
async def db():
    """A clean schema per test, on whatever DATABASE_URL is configured.

    The suite defaults to SQLite because it is fast and needs no server, but
    SQLite is not the database this product ships on. Point DATABASE_URL at
    Postgres and the whole suite runs there instead — which is the run that
    actually tells you the shipping configuration works:

        DATABASE_URL=postgresql+asyncpg://user@host/db pytest

    The `dispose()` below is what makes that possible. pytest-asyncio gives
    each test its own event loop, and an asyncpg connection is bound to the
    loop that opened it; a pooled connection surviving into the next test is
    used from a loop that has since closed, and every test after the first
    dies with "Event loop is closed". SQLite's driver does not care, so this
    was invisible until the suite was pointed at Postgres.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        async with SessionLocal() as session:
            yield session
    finally:
        # Inside the test's own loop, while it is still running.
        await engine.dispose()


@pytest_asyncio.fixture
async def company(db):
    row = Company(name="Test Company", slug="test-company")
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def project(db, company):
    row = Project(company_id=company.id, name="Test Project", slug="test-project")
    db.add(row)
    await db.flush()
    db.add(ProjectPolicy(company_id=company.id, project_id=row.id))
    await db.commit()
    return row


@pytest_asyncio.fixture
async def user(db, company, project):
    row = User(email="tester@example.com", display_name="Tester")
    db.add(row)
    await db.flush()
    db.add(
        CompanyMembership(
            company_id=company.id, user_id=row.id, role=CompanyRole.COMPANY_ADMIN
        )
    )
    db.add(
        ProjectMembership(
            company_id=company.id,
            project_id=project.id,
            user_id=row.id,
            role=ProjectRole.PROJECT_ADMIN,
        )
    )
    await db.commit()
    return row


@pytest_asyncio.fixture
async def membership(db, project, user):
    from sqlalchemy import select

    return (
        await db.execute(
            select(ProjectMembership).where(
                ProjectMembership.project_id == project.id,
                ProjectMembership.user_id == user.id,
            )
        )
    ).scalar_one()


@pytest_asyncio.fixture
async def connection(db, user, company, project):
    secret_ctx = SecretContext(
        company_id=company.id, project_id=project.id, purpose="salesforce_token"
    )
    conn = SalesforceConnection(
        company_id=company.id,
        project_id=project.id,
        connected_by=user.id,
        label="Sandbox",
        environment=Environment.SANDBOX,
        sf_org_id="00D000000000001",
        username="admin@example.com",
        instance_url="https://example.my.salesforce.com",
        login_url="https://test.salesforce.com",
        is_sandbox=True,
        org_type="sandbox",
        api_version="62.0",
        access_token_ref=store_secret("fake-access-token", secret_ctx),
        refresh_token_ref=store_secret("fake-refresh-token", secret_ctx),
    )
    db.add(conn)
    await db.commit()
    return conn


@pytest_asyncio.fixture
async def production_connection(db, user, company, project):
    """A production org, for tests that assert the production posture rules."""
    secret_ctx = SecretContext(
        company_id=company.id, project_id=project.id, purpose="salesforce_token"
    )
    conn = SalesforceConnection(
        company_id=company.id,
        project_id=project.id,
        connected_by=user.id,
        label="Production",
        environment=Environment.PRODUCTION,
        sf_org_id="00D000000000002",
        username="admin@example.com",
        instance_url="https://prod.my.salesforce.com",
        login_url="https://login.salesforce.com",
        is_sandbox=False,
        org_type="production",
        api_version="62.0",
        access_token_ref=store_secret("fake-access-token", secret_ctx),
    )
    db.add(conn)
    await db.commit()
    return conn


@pytest_asyncio.fixture
async def conversation(db, user, connection, company, project):
    conv = Conversation(
        company_id=company.id,
        project_id=project.id,
        user_id=user.id,
        salesforce_connection_id=connection.id,
    )
    db.add(conv)
    await db.commit()
    return conv


@pytest_asyncio.fixture
async def sf_client(connection, db, fake_sf):
    client = SalesforceClient(connection, db, http=fake_sf.client())
    yield client
    await client._http.aclose()
