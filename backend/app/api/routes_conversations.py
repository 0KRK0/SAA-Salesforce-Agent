"""Conversation CRUD."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.models import AgentRun, Conversation, Message, SalesforceConnection
from app.security.auth import DbSession, Tenant
from app.tenancy import service as tenancy

router = APIRouter(prefix="/conversations", tags=["conversations"])


class ConversationCreate(BaseModel):
    title: str = "New conversation"
    salesforce_connection_id: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = None
    salesforce_connection_id: str | None = None


class ConversationOut(BaseModel):
    id: str
    title: str
    salesforce_connection_id: str | None
    created_at: str
    updated_at: str


def _out(c: Conversation) -> ConversationOut:
    return ConversationOut(
        id=c.id,
        title=c.title,
        salesforce_connection_id=c.salesforce_connection_id,
        created_at=c.created_at.isoformat(),
        updated_at=c.updated_at.isoformat(),
    )


@router.get("", response_model=list[ConversationOut])
async def list_conversations(tenant: Tenant, db: DbSession) -> list[ConversationOut]:
    rows = (
        (
            await db.execute(
                select(Conversation)
                .where(
                    Conversation.project_id == tenant.project_id,
                    Conversation.user_id == tenant.user_id,
                )
                .order_by(Conversation.updated_at.desc())
                .limit(100)
            )
        )
        .scalars()
        .all()
    )
    return [_out(c) for c in rows]


@router.post("", response_model=ConversationOut)
async def create_conversation(
    payload: ConversationCreate, tenant: Tenant, db: DbSession
) -> ConversationOut:
    if payload.salesforce_connection_id:
        conn = await tenancy.owned(
            db,
            SalesforceConnection,
            payload.salesforce_connection_id,
            tenant.project_id,
        )
        if conn is None:
            raise HTTPException(status_code=404, detail="Connection not found")
    conversation = Conversation(
        company_id=tenant.company_id,
        project_id=tenant.project_id,
        user_id=tenant.user_id,
        title=payload.title,
        salesforce_connection_id=payload.salesforce_connection_id,
    )
    db.add(conversation)
    await db.commit()
    return _out(conversation)


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: str, tenant: Tenant, db: DbSession
) -> dict[str, Any]:
    conversation = await _owned(db, tenant.project_id, conversation_id)
    messages = (
        (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
        )
        .scalars()
        .all()
    )
    runs = (
        (
            await db.execute(
                select(AgentRun)
                .where(AgentRun.conversation_id == conversation_id)
                .order_by(AgentRun.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {
        "conversation": _out(conversation).model_dump(),
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "text": m.text,
                "agent_run_id": m.agent_run_id,
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "runs": [
            {
                "id": r.id,
                "state": r.state.value,
                "steps_used": r.steps_used,
                "error": r.error,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "created_at": r.created_at.isoformat(),
            }
            for r in runs
        ],
    }


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def patch_conversation(
    conversation_id: str, payload: ConversationPatch, tenant: Tenant, db: DbSession
) -> ConversationOut:
    conversation = await _owned(db, tenant.project_id, conversation_id)
    if payload.title is not None:
        conversation.title = payload.title
    if payload.salesforce_connection_id is not None:
        conn = await tenancy.owned(
            db,
            SalesforceConnection,
            payload.salesforce_connection_id,
            tenant.project_id,
        )
        if conn is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        conversation.salesforce_connection_id = payload.salesforce_connection_id
    await db.commit()
    return _out(conversation)


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, tenant: Tenant, db: DbSession
) -> dict[str, bool]:
    conversation = await _owned(db, tenant.project_id, conversation_id)
    await db.delete(conversation)
    await db.commit()
    return {"success": True}


async def _owned(db: Any, project_id: str, conversation_id: str) -> Conversation:
    """Project-scoped fetch. A conversation id from another project reads as
    'not found' — never as someone else's conversation."""
    conversation = await tenancy.owned(db, Conversation, conversation_id, project_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation
