"""
Initial Vexer intelligence schema (mandate §11, §15, §17, §21, §23, §24).

This revision deliberately contains **no copied DDL**. It executes
``vexer_platform.persistence.schema.iter_ddl()`` — the same function the contract-parity test
asserts against and the same function :meth:`PostgresStore.apply_schema` uses for ephemeral
environments. One definition, three consumers, no drift.

Upgrade notes
-------------
* Enum creation is guarded (``DO`` block), so re-running is safe.
* Table and index creation use ``IF NOT EXISTS`` for the same reason.
* ``event`` partitions are generated relative to the deploy date. Keeping partitions ahead of the
  horizon is an operational task (see ``docs/data-model.md``), not something a single migration can
  do forever.

Downgrade
---------
Destructive by nature and only ever run against a disposable database: the mandate (§54) requires an
explicit migration strategy before dropping data, and this revision documents the consequence rather
than hiding it. ``downgrade()`` drops partitions first, then tables, then the enum types.
"""
from alembic import op

from vexer_platform.persistence import schema as vexer_schema

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

#: Kept in the module so the reverse order is explicit and reviewable.
_TABLES_REVERSE = tuple(reversed(vexer_schema.TABLES))


def upgrade() -> None:
    for statement in vexer_schema.iter_ddl(months=24):
        op.execute(statement)


def downgrade() -> None:
    # Partitions (and their parent) go first: dropping the parent with attached partitions fails.
    op.execute("DROP TABLE IF EXISTS event CASCADE")
    for table in _TABLES_REVERSE:
        if table in ("event", "provenance_edge", "event_dedup"):
            continue
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for enum_name in vexer_schema.ENUM_VALUES:
        op.execute(f"DROP TYPE IF EXISTS {enum_name} CASCADE")
