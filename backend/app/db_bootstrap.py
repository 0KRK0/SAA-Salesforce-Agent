"""Reconciling a database that exists with a migration history that doesn't.

There are two ways this schema can come into being, and until now they did not
know about each other:

  * `init_db()` — `Base.metadata.create_all`, which builds the current schema
    directly from the models. Fast, needs no migration history, and used by
    every test and every local run.
  * `alembic upgrade head` — the real path, which builds the same schema one
    revision at a time and records where it got to in `alembic_version`.

A database created the first way is *at* head but does not **say** so. Point
Alembic at it and the first revision tries to `CREATE TYPE risklevel` against a
database that already has one, and the container dies on boot with
`DuplicateObjectError`. That is not a hypothetical: it is what happens to
anyone whose volume was created by a build that predates the migration
entrypoint, which is every early deployment of this product.

The bad answers are to wipe the volume (fine for a laptop, unacceptable for a
customer) or to blindly `stamp head` (fast, and silently marks a schema as
migrated when it may be nothing of the sort — every later migration then runs
against a shape it does not expect).

So this module does the one thing that is both safe and automatic: it compares
the live schema against the models and **only** stamps when they actually
match. When they don't, it refuses and says precisely what differs, because a
half-migrated production database is worse than a container that won't start.

The root cause is closed at the other end too — `init_db()` now stamps what it
creates, so the unstamped state cannot be manufactured again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app import models  # noqa: F401  — registers every mapper onto Base.metadata
from app.db import Base

# `Base.metadata` is populated as a side effect of importing the models. Without
# that import above, the metadata is empty, every comparison below finds nothing
# to compare, and a database full of tables is reported as empty — which is the
# one answer that lets the broken upgrade proceed.

#: Differences that mean the schema is genuinely not head. Alembic's comparison
#: also reports cosmetic divergence — a server default rendered differently, an
#: index Postgres named itself — which `create_all` and a migration will
#: legitimately disagree about. Those are logged, never fatal. A missing table
#: or column is a different matter: stamping over one guarantees that a later
#: migration runs against a shape it was not written for.
STRUCTURAL = ("add_table", "remove_table", "add_column", "remove_column")


def _alembic_config() -> Config:
    root = Path(__file__).resolve().parent.parent
    return Config(str(root / "alembic.ini"))


def head_revision() -> str:
    script = ScriptDirectory.from_config(_alembic_config())
    revision = script.get_current_head()
    if revision is None:  # pragma: no cover - only with no migrations at all
        raise RuntimeError("No Alembic head revision found.")
    return revision


def _has_table(conn: Connection, name: str) -> bool:
    from sqlalchemy import inspect

    return inspect(conn).has_table(name)


def _known_tables_present(conn: Connection) -> list[str]:
    from sqlalchemy import inspect

    live = set(inspect(conn).get_table_names())
    return sorted(t for t in Base.metadata.tables if t in live)


def _differences(conn: Connection) -> list[Any]:
    context = MigrationContext.configure(
        conn, opts={"compare_type": False, "compare_server_default": False}
    )
    return list(compare_metadata(context, Base.metadata))


def _describe(diff: Any) -> str:
    """A readable one-liner for one autogenerate difference."""
    if isinstance(diff, list):  # nested column-level diffs
        return "; ".join(_describe(d) for d in diff)
    if not isinstance(diff, tuple) or not diff:
        return str(diff)
    kind = diff[0]
    if kind in ("add_table", "remove_table"):
        return f"{kind}: {getattr(diff[1], 'name', diff[1])}"
    if kind in ("add_column", "remove_column"):
        return f"{kind}: {diff[2]}.{getattr(diff[3], 'name', diff[3])}"
    return kind


def stamp(conn: Connection, revision: str | None = None) -> str:
    """Record `revision` (default: head) as the applied migration.

    Written directly rather than through `alembic stamp` so it can share the
    caller's connection and transaction — the stamp and whatever created the
    schema must land together or not at all.
    """
    target = revision or head_revision()
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        )
    )
    conn.execute(text("DELETE FROM alembic_version"))
    conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": target})
    return target


class SchemaDrift(RuntimeError):
    """The live schema is unstamped *and* does not match the models."""


def reconcile(conn: Connection) -> dict[str, Any]:
    """Make an existing schema safe for `alembic upgrade head`, or refuse.

    Returns a small report describing what it decided and why. Raises
    `SchemaDrift` only in the case where guessing would be dangerous.
    """
    if _has_table(conn, "alembic_version"):
        current = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        return {
            "action": "none",
            "reason": "already under migration control",
            "revision": current,
        }

    present = _known_tables_present(conn)
    if not present:
        return {
            "action": "none",
            "reason": "empty database; migrations will build it",
        }

    diffs = _differences(conn)
    blocking = [d for d in diffs if isinstance(d, tuple) and d and d[0] in STRUCTURAL]
    # Column-level diffs arrive nested inside a list.
    for group in (d for d in diffs if isinstance(d, list)):
        blocking.extend(d for d in group if isinstance(d, tuple) and d and d[0] in STRUCTURAL)

    if blocking:
        raise SchemaDrift(
            "This database has "
            f"{len(present)} of this application's tables but no migration "
            "history, and its schema does not match the current models. It "
            "cannot be stamped safely, because a later migration would then "
            "run against a shape it was not written for.\n\n"
            "Differences found:\n  - "
            + "\n  - ".join(sorted({_describe(d) for d in blocking})[:20])
            + "\n\nResolve it deliberately: migrate the data into a fresh "
            "database, or bring the schema to the current models by hand and "
            "then run `alembic stamp head`."
        )

    revision = stamp(conn)
    return {
        "action": "stamped",
        "reason": (
            "schema was created by init_db() and matches the models exactly, "
            "so it is head; recording that rather than replaying migrations "
            "over objects that already exist"
        ),
        "revision": revision,
        "tables": len(present),
        "cosmetic_differences": len(diffs) - len(blocking),
    }


async def _run() -> dict[str, Any]:
    """Open the application's own engine and reconcile through it.

    Deliberately the async engine, not a sync one built by stripping `+asyncpg`
    off the URL. That strip looks harmless and resolves to psycopg2, which this
    application does not depend on and does not install — so the tidier-looking
    version crashes on `ModuleNotFoundError` inside the container, at the exact
    moment it is supposed to be preventing a crash.
    """
    from app.db import engine

    try:
        async with engine.begin() as conn:
            return await conn.run_sync(reconcile)
    finally:
        await engine.dispose()


def main() -> int:
    """CLI used by the container entrypoint, before `alembic upgrade head`."""
    import asyncio

    try:
        report = asyncio.run(_run())
    except SchemaDrift as exc:
        print(json.dumps({"event": "startup.schema_drift", "level": "error", "detail": str(exc)}))
        return 1

    print(json.dumps({"event": "startup.schema_baseline", "level": "info", **report}))
    return 0


if __name__ == "__main__":  # pragma: no cover - container entrypoint
    raise SystemExit(main())
