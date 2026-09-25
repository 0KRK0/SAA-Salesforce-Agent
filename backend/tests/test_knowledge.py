"""Org knowledge: what gets stored, what is refused, and how recall is bounded."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.knowledge import store
from app.models import KnowledgeKind, OrgKnowledge


async def _seed(db, project, connection, **kwargs):
    return await store.remember(
        db,
        company_id=project.company_id,
        project_id=project.id,
        salesforce_connection_id=connection.id,
        **kwargs,
    )


async def test_knowledge_from_the_model_is_refused(db, project, connection):
    """An agent that stores its own conclusions as facts convinces itself of
    something false and then acts on it."""
    with pytest.raises(ValueError) as exc:
        await _seed(
            db,
            project,
            connection,
            kind=KnowledgeKind.PATTERN,
            key="hunch",
            summary="I think the Tier field is unused",
            source="model",
        )
    assert "trusted knowledge source" in str(exc.value)


async def test_repeated_observation_refreshes_rather_than_duplicating(
    db, project, connection
):
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Account", summary="10 fields",
    )
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Account", summary="12 fields",
    )
    await db.commit()
    rows = await store.recent(db, salesforce_connection_id=connection.id)
    assert len(rows) == 1
    assert rows[0]["summary"] == "12 fields"


async def test_recall_matches_on_the_subject_of_the_request(db, project, connection):
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Opportunity",
        summary="Opportunity has 42 fields including Probability",
    )
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Contact", summary="Contact has 30 fields",
    )
    await db.commit()

    recalled = await store.recall(
        db, salesforce_connection_id=connection.id, query="Create a flow on Opportunity"
    )
    assert [r["key"] for r in recalled] == ["Opportunity"]


async def test_recall_returns_nothing_when_nothing_is_relevant(
    db, project, connection
):
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Contact", summary="Contact has 30 fields",
    )
    await db.commit()
    assert await store.recall(
        db, salesforce_connection_id=connection.id, query="quarterly revenue targets"
    ) == []


async def test_past_failures_outrank_ordinary_facts_on_an_equal_match(
    db, project, connection
):
    """Knowing a change failed here before is exactly what prevents repeating it."""
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Account",
        summary="Account schema observed",
    )
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.FAILURE, key="Account",
        summary="Deploying a field to Account failed: insufficient access",
        source="deployment",
    )
    await db.commit()
    recalled = await store.recall(
        db, salesforce_connection_id=connection.id, query="Account field deployment"
    )
    assert recalled[0]["kind"] == KnowledgeKind.FAILURE.value


async def test_expired_knowledge_is_not_recalled(db, project, connection):
    row = await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Account", summary="Stale schema",
    )
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.commit()
    assert await store.recall(
        db, salesforce_connection_id=connection.id, query="Account schema"
    ) == []


async def test_recall_respects_the_character_budget(db, project, connection):
    for i in range(40):
        await _seed(
            db, project, connection,
            kind=KnowledgeKind.OBJECT, key=f"Account_{i}",
            summary="Account " + "detail " * 40,
        )
    await db.commit()
    recalled = await store.recall(
        db, salesforce_connection_id=connection.id, query="Account", budget=1000
    )
    total = sum(len(r["summary"]) for r in recalled)
    assert total <= 1000
    assert recalled  # the budget bounds it, it does not empty it


async def test_recall_counts_a_hit_so_useful_entries_rank_higher_over_time(
    db, project, connection
):
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.OBJECT, key="Account", summary="Account schema",
    )
    await db.commit()
    await store.recall(db, salesforce_connection_id=connection.id, query="Account")
    await db.commit()
    row = (await db.execute(_select_all())).scalars().first()
    assert row.hit_count == 1


def _select_all():
    from sqlalchemy import select

    return select(OrgKnowledge)


async def test_forget_removes_an_entry(db, project, connection):
    await _seed(
        db, project, connection,
        kind=KnowledgeKind.PATTERN, key="release process", summary="Thursdays only",
        source="user",
    )
    await db.commit()
    assert await store.forget(
        db, salesforce_connection_id=connection.id, kind=KnowledgeKind.PATTERN,
        key="release process",
    )
    assert not await store.forget(
        db, salesforce_connection_id=connection.id, kind=KnowledgeKind.PATTERN, key="missing"
    )


async def test_a_failed_deployment_is_recorded_as_a_failure(db, project, connection):
    await store.record_deployment(
        db,
        company_id=project.company_id,
        project_id=project.id,
        salesforce_connection_id=connection.id,
        label="create_field: Tier__c",
        succeeded=False,
        detail={"message": "insufficient access"},
    )
    await db.commit()
    rows = await store.recent(db, salesforce_connection_id=connection.id)
    assert rows[0]["kind"] == KnowledgeKind.FAILURE.value
    assert "FAILED" in rows[0]["summary"]


async def test_schema_knowledge_records_shape_not_contents(db, project, connection):
    await store.record_schema(
        db,
        company_id=project.company_id,
        project_id=project.id,
        salesforce_connection_id=connection.id,
        object_name="Account",
        describe={
            "name": "Account",
            "label": "Account",
            "fields": [
                {"name": "Name", "custom": False},
                {"name": "Tier__c", "custom": True},
            ],
        },
    )
    await db.commit()
    rows = await store.recent(db, salesforce_connection_id=connection.id)
    assert "Tier__c" in rows[0]["summary"]
    assert "2 fields" in rows[0]["summary"]


def test_the_prompt_rendering_warns_that_knowledge_can_be_stale():
    rendered = store.render_for_prompt(
        [{"kind": "OBJECT", "key": "Account", "summary": "10 fields"}]
    )
    assert "may be out of date" in rendered
    assert "never as a substitute for inspecting" in rendered
    assert "Account" in rendered


def test_rendering_nothing_produces_nothing():
    assert store.render_for_prompt([]) == ""


def test_stopwords_do_not_drive_relevance():
    assert "the" not in store.terms_of("the Account object")
    assert "account" in store.terms_of("the Account object")
