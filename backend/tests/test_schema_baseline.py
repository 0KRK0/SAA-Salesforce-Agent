"""Reconciling a database that exists with a migration history that doesn't.

This is the defect that took the container down on a real machine: a Postgres
volume created by an earlier build held a schema built by `init_db()` and an
empty `alembic_version`. The new entrypoint ran `alembic upgrade head` against
it, the first revision reached `CREATE TYPE risklevel`, and the type was already
there. The backend exited 1 on every boot.

Nothing in the 620 tests could have caught it. They all start from an empty
database, and the bug lives entirely in the state *between* two ways of
building a schema. So these tests start from the broken state on purpose.

The property being protected: **a schema is never stamped unless it actually
matches the models.** Stamping is the fast fix and the dangerous one — it marks
a database as migrated, and every later revision then runs against a shape it
was not written for. Refusing to boot is the correct outcome when the two
disagree.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db import Base
from app.db_bootstrap import SchemaDrift, head_revision, reconcile, stamp


@pytest.fixture
def legacy_db(tmp_path):
    """A database built the way `init_db()` used to build one: no stamp."""
    engine = create_engine(f"sqlite:///{tmp_path/'legacy.db'}", future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def empty_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'empty.db'}", future=True)
    yield engine
    engine.dispose()


def _version(engine) -> str | None:
    with engine.begin() as conn:
        if not inspect(conn).has_table("alembic_version"):
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


# ---------------------------------------------------------------------------
# The failing case
# ---------------------------------------------------------------------------
def test_a_schema_that_matches_the_models_is_recognised_as_head(legacy_db):
    """`create_all` produces exactly what the newest migration produces — it is
    built from the same models. The only thing missing is the record of it."""
    assert _version(legacy_db) is None

    with legacy_db.begin() as conn:
        report = reconcile(conn)

    assert report["action"] == "stamped"
    assert _version(legacy_db) == head_revision()


def test_the_stamp_is_the_real_alembic_head_not_a_hardcoded_string():
    """A new migration must move this automatically. A literal revision id here
    would silently stamp every future deployment one revision behind."""
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    script = ScriptDirectory.from_config(Config(str(root / "alembic.ini")))
    assert head_revision() == script.get_current_head()


# ---------------------------------------------------------------------------
# The case where stamping would be dangerous
# ---------------------------------------------------------------------------
def test_a_schema_missing_a_table_is_refused_not_stamped(legacy_db):
    """The whole point. Stamping here would mark a database as migrated when a
    later revision expects a table that is not there."""
    with legacy_db.begin() as conn:
        conn.execute(text("DROP TABLE mcp_servers"))

    with legacy_db.begin() as conn, pytest.raises(SchemaDrift) as caught:
        reconcile(conn)

    assert "mcp_servers" in str(caught.value)
    assert _version(legacy_db) is None, "a refused database must be left untouched"


def test_a_schema_missing_a_column_is_refused_too(legacy_db):
    """Column-level drift arrives nested inside a list in Alembic's diff output
    and was missed by the first version of the check."""
    with legacy_db.begin() as conn:
        # SQLite refuses to drop a column an index still references, so the
        # index goes first. Both are then missing, which is exactly the shape
        # a partially hand-patched database has.
        conn.execute(text("DROP INDEX ix_agent_runs_correlation_id"))
        conn.execute(text("ALTER TABLE agent_runs DROP COLUMN correlation_id"))

    with legacy_db.begin() as conn, pytest.raises(SchemaDrift) as caught:
        reconcile(conn)

    assert "correlation_id" in str(caught.value)


def test_the_refusal_names_what_differs(legacy_db):
    """An operator reading a boot failure at 3am needs the difference, not a
    stack trace ending in DuplicateObjectError."""
    with legacy_db.begin() as conn:
        conn.execute(text("DROP TABLE mcp_servers"))

    with legacy_db.begin() as conn, pytest.raises(SchemaDrift) as caught:
        reconcile(conn)

    message = str(caught.value)
    assert "no migration history" in message
    assert "alembic stamp head" in message, "say how to resolve it deliberately"


# ---------------------------------------------------------------------------
# The cases that must stay untouched
# ---------------------------------------------------------------------------
def test_an_empty_database_is_left_for_the_migrations_to_build(empty_db):
    with empty_db.begin() as conn:
        report = reconcile(conn)

    assert report["action"] == "none"
    assert "empty" in report["reason"]
    assert _version(empty_db) is None


def test_a_database_already_under_alembic_is_never_restamped(legacy_db):
    """Rewriting the version of a database mid-migration would skip revisions."""
    with legacy_db.begin() as conn:
        stamp(conn, "4f03de264731")

    with legacy_db.begin() as conn:
        report = reconcile(conn)

    assert report["action"] == "none"
    assert report["revision"] == "4f03de264731"
    assert _version(legacy_db) == "4f03de264731", "an older revision must survive"


# ---------------------------------------------------------------------------
# The root cause, closed at the other end
# ---------------------------------------------------------------------------
async def test_init_db_stamps_the_schema_it_creates(tmp_path, monkeypatch):
    """The unstamped state cannot be manufactured any more.

    `init_db()` builds the current schema and now records that fact in the same
    transaction, so the two ways of creating this database agree about where it
    stands.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    import app.db as db_module

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'fresh.db'}")
    monkeypatch.setattr(db_module, "engine", engine)
    try:
        await db_module.init_db()
        async with engine.begin() as conn:
            version = await conn.run_sync(
                lambda sync: sync.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
            )
    finally:
        await engine.dispose()

    assert version == head_revision()
