"""Alembic environment configuration.

Wired to use the application's SQLAlchemy ``Base.metadata`` and
``settings.database_url`` so that ``alembic revision --autogenerate``
picks up all ORM models automatically.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.config import settings
from app.database import Base

# ── Import all model modules here so Base.metadata is fully populated ──
# Importing the package triggers all model registrations in the correct order:
#   Merchant (no FK) → Transaction (FK→Merchant) → Event (FK→Transaction)
import app.models  # noqa: F401

# Alembic Config object (reads alembic.ini)
config = context.config

# Override sqlalchemy.url with the value from application settings,
# so the single source of truth is always .env / environment variables.
config.set_main_option("sqlalchemy.url", settings.database_url)

# Set up Python logging from alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata target for autogenerate support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — generates SQL script without DB connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode — connects to the database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
