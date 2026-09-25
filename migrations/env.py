"""
Alembic environment for the Vexer platform (mandate §4 — reproducible, no undocumented local state).

The database URL is **never** read from ``alembic.ini`` and never committed: it comes from the
``VEXER_POSTGRES_DSN`` environment variable, or from ``sqlalchemy.url`` only when a developer sets
``sqlalchemy.url`` in a *local, git-ignored* override. This keeps §29/§32 intact — no credentials in
source control, no silent default of ``localhost`` with default passwords.

Run offline (SQL only, for review in a change pipeline)::

    alembic upgrade head --sql

Run online against a real server::

    set VEXER_POSTGRES_DSN=postgresql://vexer:...@localhost:5432/vexer
    alembic upgrade head
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the repository importable so migrations can import the canonical DDL module rather than
# duplicating it (the whole point of vexer_platform.persistence.schema being the single source).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vexer_platform.persistence import schema as vexer_schema  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

#: Kept for offline mode only; migrations are executed as raw DDL from the canonical module.
target_metadata = None

ENV_DSN = "VEXER_POSTGRES_DSN"


def _database_url() -> str:
    url = os.environ.get(ENV_DSN) or config.get_main_option("sqlalchemy.url", "")
    if not url:
        raise SystemExit(
            f"{ENV_DSN} is not set. Refusing to guess a database target: set the DSN explicitly "
            "(see docs/data-model.md). No credential is ever defaulted or committed."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — used to review a migration before applying it."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect with a pooled engine and run migrations in a transaction."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()


def _schema_ddl():  # pragma: no cover - re-exported for `alembic revision` templates
    return vexer_schema.iter_ddl()
