"""Redaction, retention, cost limits and the posture report.

The theme: **a promise that nothing enforces is not a control.** Each of these
was a sentence in a policy row or a README before this phase, and each is now a
thing that runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    AgentRun,
    AuditEvent,
    Message,
    ProjectMembership,
    ProjectRole,
    RunEvent,
    RunState,
    ToolExecution,
    User,
)
from app.observability import redaction
from app.retention import MIN_AUDIT_DAYS, describe, sweep_project
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


@pytest.fixture
def auth(user, project):
    return {
        "Authorization": f"Bearer {create_session_token(user.id, project.id)}",
        "X-Project-Id": project.id,
    }


# ---------------------------------------------------------------------------
# Redaction — key names
# ---------------------------------------------------------------------------
def test_a_key_named_like_a_secret_is_hidden_whatever_its_value():
    out = redaction.redact({"access_token": "anything", "object": "Account"})
    assert out["access_token"] == redaction.REDACTED
    assert out["object"] == "Account"


def test_a_prefixed_secret_key_is_still_a_secret_key():
    """`salesforce_access_token` and `jira_client_secret` are the names these
    actually have in this codebase."""
    out = redaction.redact(
        {
            "salesforce_access_token": "x",
            "jira_client_secret": "y",
            "x-api-key": "z",
            "instance_url": "https://acme.my.salesforce.com",
        }
    )
    assert out["salesforce_access_token"] == redaction.REDACTED
    assert out["jira_client_secret"] == redaction.REDACTED
    assert out["x-api-key"] == redaction.REDACTED
    assert out["instance_url"].startswith("https://")


def test_redaction_reaches_into_nested_structures():
    out = redaction.redact(
        {"connections": [{"name": "prod", "refresh_token": "secret"}]}
    )
    assert out["connections"][0]["refresh_token"] == redaction.REDACTED
    assert out["connections"][0]["name"] == "prod"


# ---------------------------------------------------------------------------
# Redaction — values
#
# The half that key-name checks miss, and the half that actually leaks.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "marker"),
    [
        ("401 for sk-ant-api03-AbCdEfGhIjKlMnOpQrSt", "anthropic"),
        ("used sk-proj-AbCdEfGhIjKlMnOpQrStUvWx1234", "openai"),
        ("Bearer ghp_16C7e42F292c6912E7710c838347Ae178B4a", "github"),
        ("token github_pat_11ABCDEFG0abcdefghijkl", "github_fine_grained"),
        ("key AIzaSyA1234567890abcdefghijklmnopqrstuv", "google"),
        ("xoxb-1234567890-abcdefghijkl", "slack"),
        ("AKIAIOSFODNN7EXAMPLE", "aws_key_id"),
        ("SFDX_AUTH_URL=force://PlatformCLI::5Aep861xyz@na1.salesforce.com", "sfdx_auth_url"),
        ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop", "jwt"),
        ("ref local:v1:AbCdEfGhIjKlMnOpQrStUvWxYz", "secret_reference"),
    ],
)
def test_a_credential_inside_a_value_is_scrubbed(text, marker):
    """These are the shapes that end up in an error message, a stack trace or
    command output — places nothing is guarding."""
    cleaned = redaction.scrub(text)
    assert f"<redacted:{marker}>" in cleaned
    assert text.split()[-1] not in cleaned or marker == "sfdx_auth_url"


def test_a_salesforce_session_id_is_scrubbed():
    session = "00D5j000001abcD!AQEAQK9abcdefghijklmnopqrstuvwxyz1234"
    assert "<redacted:salesforce_session>" in redaction.scrub(f"sid={session}")


def test_a_private_key_block_is_scrubbed_whole():
    block = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK\n"
        "-----END RSA PRIVATE KEY-----"
    )
    assert "MIIEpAIBAAK" not in redaction.scrub(block)


def test_salesforce_identifiers_and_code_survive_redaction():
    """The reason patterns are anchored on issuer prefixes rather than entropy.

    An entropy heuristic eats record ids, SOQL and Apex — precisely the evidence
    someone reads an audit trail for."""
    keep = [
        "SELECT Id, Name FROM Account WHERE Id = '001000000000001AAA'",
        "public class AccountService { void run() { insert new Account(); } }",
        "00D5j000001abcD",  # an org id on its own is not a session id
        "0Af5j00000ABCDEcAAH",  # a deploy id
        "force-app/main/default/objects/Account/fields/Tier__c.field-meta.xml",
    ]
    for text in keep:
        assert redaction.scrub(text) == text, text


def test_a_very_long_string_is_truncated_with_its_length_stated():
    out = redaction.redact({"apex": "x" * 50_000})
    assert len(out["apex"]) < redaction.MAX_STRING + 100
    assert "50000 chars" in out["apex"]


def test_redaction_terminates_on_deeply_nested_input():
    value: dict = {"a": 1}
    for _ in range(40):
        value = {"nested": value}
    assert "truncated:depth" in str(redaction.redact(value))


def test_redaction_describes_its_own_limits():
    described = redaction.describe()
    assert described["value_patterns"]
    assert "not on entropy" in described["note"]


async def test_every_log_line_is_redacted_even_when_the_call_site_forgets():
    """The last line of defence. The one call site that forgets is the one that
    writes a token to a log file."""
    from app.observability.logging import _redact_event

    out = _redact_event(None, "info", {"event": "x", "api_key": "sk-live-1", "n": 1})
    assert out["api_key"] == "<redacted>"
    assert out["n"] == 1


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def old_data(db, project, user, conversation):
    """A finished run from a fortnight ago, with content attached."""
    old = datetime.now(UTC) - timedelta(days=14)
    run = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=user.id,
        state=RunState.COMPLETED,
        final_text="Created Account.Tier__c.",
        transcript=[{"role": "user", "content": "add a field"}],
        created_at=old,
    )
    db.add(run)
    await db.flush()
    db.add_all(
        [
            Message(
                conversation_id=conversation.id,
                role="user",
                text="add a field",
                agent_run_id=run.id,
                created_at=old,
            ),
            RunEvent(
                company_id=project.company_id,
                project_id=project.id,
                agent_run_id=run.id,
                sequence=1,
                event_type="tool.finished",
                data={"records": [{"Name": "Acme"}]},
                created_at=old,
            ),
            ToolExecution(
                company_id=project.company_id,
                project_id=project.id,
                agent_run_id=run.id,
                conversation_id=conversation.id,
                user_id=user.id,
                tool_name="create_field",
                salesforce_object="Account",
                arguments={"object": "Account"},
                result={"success": True},
                created_at=old,
            ),
            AuditEvent(
                company_id=project.company_id,
                project_id=project.id,
                user_id=user.id,
                agent_run_id=run.id,
                action="tool.create_field",
                salesforce_object="Account",
                arguments={"object": "Account"},
                result_summary={"success": True},
                outcome="ok",
                created_at=old,
            ),
        ]
    )
    await db.commit()
    return run


async def test_a_dry_run_deletes_nothing(db, project, old_data):
    from app.tenancy.service import policy_row

    policy = await policy_row(db, project.id, project.company_id)
    result = await sweep_project(db, project, policy, dry_run=True)
    await db.commit()

    assert result.total() > 0  # it reports what it *would* do
    await db.refresh(old_data)
    assert old_data.transcript is not None
    assert (await db.execute(select(Message))).scalars().all()


async def test_the_default_policy_deletes_content_once_the_work_is_finished(
    db, project, old_data
):
    """Default retention is zero days. "Processed and discarded" has to be a job
    that runs, not a sentence in a README."""
    from app.tenancy.service import policy_row

    policy = await policy_row(db, project.id, project.company_id)
    result = await sweep_project(db, project, policy)
    await db.commit()

    await db.refresh(old_data)
    assert old_data.transcript is None
    # What the user was told survives: a run that reports nothing is
    # indistinguishable from one that never happened.
    assert old_data.final_text == "Created Account.Tier__c."
    assert result.messages_deleted >= 1
    assert result.run_events_deleted >= 1


async def test_tool_payloads_go_but_the_operation_record_stays(db, project, old_data):
    from app.tenancy.service import policy_row

    policy = await policy_row(db, project.id, project.company_id)
    await sweep_project(db, project, policy)
    await db.commit()

    execution = (await db.execute(select(ToolExecution))).scalars().one()
    assert execution.arguments is None
    assert execution.result is None
    # Everything that identifies what happened survives.
    assert execution.tool_name == "create_field"
    assert execution.salesforce_object == "Account"


async def test_audit_payloads_go_but_the_audit_row_stays(db, project, old_data):
    """The row that says what happened, who approved it and when is what a
    compliance question is asked about. The argument blob is not."""
    from app.tenancy.service import policy_row

    policy = await policy_row(db, project.id, project.company_id)
    await sweep_project(db, project, policy)
    await db.commit()

    row = (await db.execute(select(AuditEvent))).scalars().one()
    assert row.arguments is None
    assert row.result_summary is None
    assert row.action == "tool.create_field"
    assert row.salesforce_object == "Account"


async def test_a_live_run_is_never_touched(db, project, user, conversation):
    """Deleting the transcript of a run waiting for approval would destroy the
    thing the approval authorizes."""
    from app.tenancy.service import policy_row

    old = datetime.now(UTC) - timedelta(days=30)
    live = AgentRun(
        company_id=project.company_id,
        project_id=project.id,
        conversation_id=conversation.id,
        user_id=user.id,
        state=RunState.WAITING_FOR_APPROVAL,
        transcript=[{"role": "user", "content": "pending"}],
        created_at=old,
    )
    db.add(live)
    db.add(
        Message(
            conversation_id=conversation.id,
            role="user",
            text="pending",
            agent_run_id=live.id,
            created_at=old,
        )
    )
    await db.commit()

    policy = await policy_row(db, project.id, project.company_id)
    result = await sweep_project(db, project, policy)
    await db.commit()

    await db.refresh(live)
    assert live.transcript is not None
    assert result.skipped_live_runs == 1
    assert "waiting for approval" in " ".join(result.notes)
    assert (await db.execute(select(Message))).scalars().all()


async def test_a_longer_retention_keeps_recent_content(db, project, old_data):
    from app.tenancy.service import policy_row

    policy = await policy_row(db, project.id, project.company_id)
    policy.retain_conversation_days = 90
    policy.retain_tool_payload_days = 90
    await db.commit()

    result = await sweep_project(db, project, policy)
    await db.commit()
    assert result.transcripts_cleared == 0
    assert result.messages_deleted == 0


async def test_audit_rows_have_a_floor_no_policy_can_go_under(db, project, old_data):
    """A policy edit should not be able to erase last week's approvals."""
    from app.tenancy.service import policy_row

    policy = await policy_row(db, project.id, project.company_id)
    policy.audit_retention_days = 1
    await db.commit()

    result = await sweep_project(db, project, policy)
    await db.commit()
    # 14 days old, floor is 30 — the row survives.
    assert result.audit_rows_deleted == 0
    assert (await db.execute(select(AuditEvent))).scalars().all()


def test_retention_is_described_in_sentences_not_settings():
    """A compliance answer that reads as a settings dump is not an answer."""
    described = describe(None)
    assert described["explained"]
    assert any("deleted as soon as" in line for line in described["explained"])
    assert described["audit_minimum_days"] == MIN_AUDIT_DAYS
    assert described["never_stored"]


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------
async def test_metrics_report_no_data_rather_than_a_misleading_zero(client, auth):
    """A 0% failure rate over zero runs reads as "nothing is failing"."""
    body = (await client.get(f"{API}/operations/metrics", headers=auth)).json()
    assert body["runs"]["total"] == 0
    assert body["runs"]["failure_rate"] is None
    assert body["approvals"]["rejection_rate"] is None


async def test_metrics_compute_a_rate_once_there_is_data(
    client, auth, db, project, user, conversation
):
    for state in (RunState.COMPLETED, RunState.COMPLETED, RunState.FAILED):
        db.add(
            AgentRun(
                company_id=project.company_id,
                project_id=project.id,
                conversation_id=conversation.id,
                user_id=user.id,
                state=state,
            )
        )
    await db.commit()

    body = (await client.get(f"{API}/operations/metrics", headers=auth)).json()
    assert body["runs"]["total"] == 3
    assert body["runs"]["failure_rate"] == pytest.approx(1 / 3, abs=0.001)


async def test_the_sweep_endpoint_defaults_to_a_dry_run(client, auth, db, old_data):
    """Deleting a customer's history is not something an endpoint should do
    because someone was curious what the button did."""
    body = (await client.post(f"{API}/operations/retention/sweep", headers=auth)).json()
    assert body["dry_run"] is True
    assert "Nothing was deleted" in body["message"]
    await db.refresh(old_data)
    assert old_data.transcript is not None


async def test_applying_the_sweep_is_audited(client, auth, db, old_data):
    body = (
        await client.post(
            f"{API}/operations/retention/sweep?dry_run=false", headers=auth
        )
    ).json()
    assert body["dry_run"] is False
    actions = [
        r.action for r in (await db.execute(select(AuditEvent))).scalars().all()
    ]
    assert "retention.swept" in actions


async def test_only_an_admin_can_apply_retention(client, db, project):
    developer = User(email="dev-retention@example.com")
    db.add(developer)
    await db.flush()
    db.add(
        ProjectMembership(
            company_id=project.company_id,
            project_id=project.id,
            user_id=developer.id,
            role=ProjectRole.DEVELOPER,
        )
    )
    await db.commit()
    resp = await client.post(
        f"{API}/operations/retention/sweep?dry_run=false",
        headers={
            "Authorization": f"Bearer {create_session_token(developer.id, project.id)}",
            "X-Project-Id": project.id,
        },
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Cost control
# ---------------------------------------------------------------------------
async def test_the_run_allowance_is_enforced_before_any_work_is_queued(
    client, auth, db, project, conversation, user
):
    """Refusing after a worker has spent a model call would charge for the
    thing being refused."""
    from app.tenancy.service import subscription_for

    subscription = await subscription_for(db, project.company_id)
    subscription.monthly_run_allowance = 1
    db.add(
        AgentRun(
            company_id=project.company_id,
            project_id=project.id,
            conversation_id=conversation.id,
            user_id=user.id,
            state=RunState.COMPLETED,
        )
    )
    await db.commit()

    resp = await client.post(
        f"{API}/conversations/{conversation.id}/messages",
        json={"message": "one more"},
        headers=auth,
    )
    assert resp.status_code == 402
    assert "used all 1 runs" in resp.json()["detail"]
    # And nothing was queued.
    queued = (
        await db.execute(select(AgentRun).where(AgentRun.state == RunState.QUEUED))
    ).scalars().all()
    assert queued == []


async def test_an_unlimited_allowance_never_refuses(
    client, auth, db, project, conversation
):
    from app.tenancy.service import subscription_for

    subscription = await subscription_for(db, project.company_id)
    subscription.monthly_run_allowance = 0
    await db.commit()

    resp = await client.post(
        f"{API}/conversations/{conversation.id}/messages",
        json={"message": "go"},
        headers=auth,
    )
    assert resp.status_code == 202


async def test_entitlements_report_usage_against_the_allowance(client, auth):
    body = (await client.get(f"{API}/operations/entitlements", headers=auth)).json()
    assert body["runs"]["allowance"] > 0
    assert body["runs"]["exhausted"] is False
    assert body["billing"]["provider_connected"] is False


# ---------------------------------------------------------------------------
# Posture
# ---------------------------------------------------------------------------
async def test_the_posture_report_is_generated_from_configuration(client, auth):
    """A security answer written down once drifts. This is read from the
    running process."""
    body = (await client.get(f"{API}/operations/posture", headers=auth)).json()
    assert body["secrets"]["backend"]
    assert body["execution"]["server_owned_runs"] is True
    assert "arguments hash" in body["approvals"]["bound_to"]
    assert body["salesforce"]["permission_ceiling"].startswith("The connected")


async def test_the_posture_report_lists_what_is_not_implemented(client, auth):
    body = (await client.get(f"{API}/operations/posture", headers=auth)).json()
    joined = " ".join(body["not_implemented"]).lower()
    assert "saml" in joined
    assert "bedrock" in joined
    assert "payment" in joined


async def test_the_posture_report_makes_no_certification_claim(client, auth):
    """Saying "SOC 2 compliant" without an attestation is the specific lie an
    enterprise buyer is most likely to be told."""
    body = (await client.get(f"{API}/operations/posture", headers=auth)).json()
    note = body["compliance_note"].lower()
    assert "not a certification claim" in note
    assert "soc 2" in note
