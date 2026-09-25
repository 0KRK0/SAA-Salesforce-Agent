#!/bin/sh
# Container entrypoint.
#
# Migrations run here rather than in a separate job so that `docker compose up`
# on a fresh machine produces a working system with no second step. That is the
# right default for one replica and the wrong one for many: Alembic takes no
# lock of its own, so several containers starting together can race.
#
# Set RUN_MIGRATIONS_ON_START=false and run `alembic upgrade head` as a
# pre-deploy job wherever more than one replica starts at once.
set -e

if [ "${RUN_MIGRATIONS_ON_START:-true}" = "true" ]; then
  # A volume created by a build that predates this entrypoint holds a schema
  # built by init_db() with no migration history. Alembic would replay its
  # first revision over objects that already exist and die on boot. This
  # reconciles that case -- and only that case -- before the upgrade runs; it
  # refuses rather than guessing when the schema does not match the models.
  echo '{"event":"startup.checking_schema","level":"info"}'
  python -m app.db_bootstrap

  echo '{"event":"startup.migrating","level":"info"}'
  alembic upgrade head
  echo '{"event":"startup.migrated","level":"info"}'
else
  echo '{"event":"startup.migrations_skipped","level":"warning","detail":"RUN_MIGRATIONS_ON_START is false; the schema must already be current."}'
fi

exec "$@"
