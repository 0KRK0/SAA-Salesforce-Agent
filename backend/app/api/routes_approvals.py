"""Human-in-the-loop approval endpoints.

An approval is only ever created by the runtime and only ever decided by an
explicit call to this endpoint. Nothing in a chat message can approve a change.

What this endpoint enforces, deterministically, before recording a vote:

  * the approval belongs to the caller's project;
  * the caller holds one of the roles the policy makes eligible;
  * separation of duties, when the project requires it;
  * the approval has not expired;
  * the caller is voting on the arguments they were shown — an edit made by an
    earlier approver invalidates any votes cast before it.

Only when the required number of distinct eligible humans have approved does
the approval reach APPROVED, and even then the runtime re-checks binding and
org state before executing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.models import (
    AgentRun,
    Approval,
    ApprovalDecision,
    ApprovalState,
    AuditEvent,
    ProjectMembership,
    RunState,
)
from app.security.auth import DbSession, Tenant
from app.tenancy import service as tenancy
from app.tenancy.policy import can_decide, change_hash, is_expired

router = APIRouter(prefix="/approvals", tags=["approvals"])


class Decision(BaseModel):
    decision: Literal["approve", "reject"]
    note: str | None = None
    modified_arguments: dict[str, Any] | None = None


def _out(
    a: Approval,
    decisions: list[ApprovalDecision] | None = None,
    *,
    viewer: Any = None,
    eligible_people: int | None = None,
) -> dict[str, Any]:
    votes = decisions if decisions is not None else []
    approvals = [d for d in votes if d.decision == "approve"]
    return {
        "id": a.id,
        "agent_run_id": a.agent_run_id,
        "conversation_id": a.conversation_id,
        "tool_name": a.tool_name,
        "risk_level": a.risk_level.value,
        "environment": a.environment.value if a.environment else None,
        "state": a.state.value,
        "arguments": a.arguments,
        "modified_arguments": a.modified_arguments,
        "plan": a.plan,
        "decision_note": a.decision_note,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "created_at": a.created_at.isoformat(),
        # --- Phase J surface ---
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "expired": is_expired(a.expires_at),
        "change_hash": a.change_hash,
        "approvals_required": a.approvals_required,
        "approvals_recorded": len(approvals),
        "eligible_roles": a.eligible_roles or [],
        "require_separate_approver": a.require_separate_approver,
        "approved_by": a.approved_by,
        "approved_at": a.approved_at.isoformat() if a.approved_at else None,
        "invalidated_reason": a.invalidated_reason,
        "requested_by": a.user_id,
        "decisions": [
            {
                "user_id": d.user_id,
                "role": d.role.value,
                "decision": d.decision,
                "note": d.note,
                "at": d.created_at.isoformat(),
            }
            for d in votes
        ],
        # Whether *this* viewer may decide, decided on the server that owns the
        # rule rather than re-derived in the browser. Without it the card
        # offered an "Approve (2/2)" button to somebody who had already voted,
        # which the API then refused — a control that promises an action and
        # then declines it reads as a broken product, not as a control.
        **_viewer_state(a, votes, viewer, eligible_people),
    }


def _viewer_state(
    a: Approval,
    votes: list[ApprovalDecision],
    viewer: Any,
    eligible_people: int | None,
) -> dict[str, Any]:
    approvals = [d for d in votes if d.decision == "approve"]
    outstanding = max(0, a.approvals_required - len(approvals))

    # A project with fewer eligible approvers than the change requires can never
    # satisfy it. Saying so is the difference between a considered control and a
    # dead end: a single-operator project would otherwise click Approve forever.
    deadlocked = (
        a.state is ApprovalState.PENDING
        and eligible_people is not None
        and outstanding > 0
        and eligible_people < a.approvals_required
    )

    state: dict[str, Any] = {
        "outstanding_approvals": outstanding,
        "eligible_approver_count": eligible_people,
        "deadlocked": deadlocked,
        "you_have_decided": False,
        "you_can_decide": False,
        "you_cannot_decide_because": "",
    }
    if viewer is None:
        return state

    state["you_have_decided"] = any(v.user_id == viewer.user_id for v in votes)
    if a.state is not ApprovalState.PENDING:
        state["you_cannot_decide_because"] = f"This approval is already {a.state.value.lower()}."
        return state
    if is_expired(a.expires_at):
        state["you_cannot_decide_because"] = "This approval has expired."
        return state

    eligibility = can_decide(
        membership=viewer.membership,
        eligible_roles=a.eligible_roles,
        requester_user_id=a.user_id,
        require_separate_approver=a.require_separate_approver,
        already_voted=state["you_have_decided"],
    )
    state["you_can_decide"] = eligibility.allowed
    if not eligibility.allowed:
        state["you_cannot_decide_because"] = eligibility.reason
    if deadlocked:
        roles = ", ".join(a.eligible_roles or []) or "an eligible role"
        state["deadlock_detail"] = (
            f"This change needs {a.approvals_required} approvals from different people, "
            f"and this project has {eligible_people} member(s) who can give one "
            f"({roles}). Invite another approver, or a project administrator can lower "
            f"the requirement for this change type under Settings. Irreversible "
            f"production operations always need two people and cannot be lowered."
        )
    return state


async def _eligible_people(db: Any, project_id: str, roles: list[str] | None) -> int:
    """How many active members of this project could approve this change."""
    stmt = select(ProjectMembership).where(
        ProjectMembership.project_id == project_id,
        ProjectMembership.is_active.is_(True),
    )
    members = (await db.execute(stmt)).scalars().all()
    if not roles:
        return len(members)
    wanted = {str(r).upper() for r in roles}
    return sum(1 for m in members if m.role.value in wanted)


async def _votes(db: Any, approval_id: str) -> list[ApprovalDecision]:
    rows = (
        await db.execute(
            select(ApprovalDecision)
            .where(ApprovalDecision.approval_id == approval_id)
            .order_by(ApprovalDecision.created_at)
        )
    ).scalars().all()
    return list(rows)


@router.get("")
async def list_approvals(
    tenant: Tenant,
    db: DbSession,
    state: str | None = Query(None),
    conversation_id: str | None = Query(None),
    limit: int = Query(50, le=200),
) -> dict[str, Any]:
    stmt = select(Approval).where(Approval.project_id == tenant.project_id)
    if state:
        stmt = stmt.where(Approval.state == ApprovalState(state.upper()))
    if conversation_id:
        stmt = stmt.where(Approval.conversation_id == conversation_id)
    rows = (
        (await db.execute(stmt.order_by(Approval.created_at.desc()).limit(limit)))
        .scalars()
        .all()
    )
    out = []
    for row in rows:
        out.append(
            _out(
                row,
                await _votes(db, row.id),
                viewer=tenant,
                eligible_people=await _eligible_people(
                    db, tenant.project_id, row.eligible_roles
                ),
            )
        )
    return {"count": len(out), "approvals": out}


@router.get("/{approval_id}")
async def get_approval(approval_id: str, tenant: Tenant, db: DbSession) -> dict[str, Any]:
    approval = await _in_project(db, tenant.project_id, approval_id)
    return _out(
        approval,
        await _votes(db, approval.id),
        viewer=tenant,
        eligible_people=await _eligible_people(
            db, tenant.project_id, approval.eligible_roles
        ),
    )


@router.post("/{approval_id}/decision")
async def decide(
    approval_id: str, payload: Decision, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    approval = await _in_project(db, tenant.project_id, approval_id)
    if approval.state != ApprovalState.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"Approval is already {approval.state.value} and cannot be changed.",
        )
    if is_expired(approval.expires_at):
        approval.state = ApprovalState.EXPIRED
        approval.invalidated_reason = "Expired before a decision was recorded."
        await db.commit()
        raise HTTPException(
            status_code=409,
            detail=(
                "This approval expired before it was decided. Ask the agent to propose "
                "the change again so it can be reviewed against current org state."
            ),
        )

    votes = await _votes(db, approval.id)
    eligibility = _check_eligibility(tenant, approval, votes)
    if not eligibility.allowed:
        raise HTTPException(status_code=403, detail=eligibility.reason)

    # An edit changes what is being approved, so earlier votes no longer apply.
    if payload.decision == "approve" and payload.modified_arguments is not None:
        if payload.modified_arguments != approval.effective_arguments():
            approval.modified_arguments = payload.modified_arguments
            approval.change_hash = change_hash(approval.tool_name, payload.modified_arguments)
            for stale in votes:
                await db.delete(stale)
            votes = []
            await db.flush()

    now = datetime.now(UTC)
    vote = ApprovalDecision(
        approval_id=approval.id,
        company_id=tenant.company_id,
        project_id=tenant.project_id,
        user_id=tenant.user_id,
        role=tenant.role,
        decision=payload.decision,
        note=payload.note,
        decided_change_hash=approval.change_hash,
    )
    db.add(vote)
    await db.flush()
    votes = [*votes, vote]

    approvals_recorded = sum(1 for v in votes if v.decision == "approve")
    if payload.decision == "reject":
        approval.state = ApprovalState.REJECTED
    elif approvals_recorded >= approval.approvals_required:
        approval.state = ApprovalState.APPROVED
        approval.approved_by = tenant.user_id
        approval.approved_at = now

    approval.decided_by = tenant.user_id
    approval.decided_at = now
    approval.decision_note = payload.note

    db.add(
        AuditEvent(
            company_id=tenant.company_id,
            project_id=tenant.project_id,
            user_id=tenant.user_id,
            salesforce_connection_id=approval.salesforce_connection_id,
            environment=approval.environment,
            conversation_id=approval.conversation_id,
            agent_run_id=approval.agent_run_id,
            action=f"approval.{payload.decision}d",
            tool_name=approval.tool_name,
            arguments=approval.effective_arguments(),
            result_summary={
                "approvals_recorded": approvals_recorded,
                "approvals_required": approval.approvals_required,
                "role": tenant.role.value,
                "change_hash": approval.change_hash,
            },
            risk_level=approval.risk_level,
            approval_state=approval.state,
            outcome="ok",
        )
    )
    await db.commit()

    run = await db.get(AgentRun, approval.agent_run_id)
    pending = (
        (
            await db.execute(
                select(Approval).where(
                    Approval.agent_run_id == approval.agent_run_id,
                    Approval.project_id == tenant.project_id,
                    Approval.state == ApprovalState.PENDING,
                )
            )
        )
        .scalars()
        .all()
    )
    return {
        "success": True,
        "approval": _out(approval, votes),
        "run_id": approval.agent_run_id,
        "run_state": run.state.value if run else None,
        "pending_approvals": [a.id for a in pending],
        "awaiting_more_approvers": (
            approval.state == ApprovalState.PENDING and payload.decision == "approve"
        ),
        "resume_ready": bool(run and run.state == RunState.WAITING_FOR_APPROVAL and not pending),
    }


def _check_eligibility(
    tenant: Any, approval: Approval, votes: list[ApprovalDecision]
) -> Any:
    from app.tenancy.policy import can_decide

    return can_decide(
        membership=tenant.membership,
        eligible_roles=approval.eligible_roles,
        requester_user_id=approval.user_id,
        require_separate_approver=approval.require_separate_approver,
        already_voted=any(v.user_id == tenant.user_id for v in votes),
    )


async def _in_project(db: Any, project_id: str, approval_id: str) -> Approval:
    approval = await tenancy.owned(db, Approval, approval_id, project_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval
