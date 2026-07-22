from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        self._ensure_sqlite_parent(url)
        self.engine: AsyncEngine = create_async_engine(url, echo=echo, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self, *, legacy_root: str | None = None) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._migrate_legacy_schema, legacy_root or os.getcwd())

    async def drop_all(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)

    async def close(self) -> None:
        await self.engine.dispose()

    @staticmethod
    def _migrate_legacy_schema(connection: Connection, legacy_root: str) -> None:
        """Idempotently upgrade v1.3 databases without requiring data loss.

        New databases are created from metadata. Existing databases receive nullable scope
        columns and a stable default repository; application writes require explicit scope once
        multiple repositories exist. The migration is deliberately backend-neutral and can be
        rerun safely during every startup.
        """
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if "repositories" not in tables:
            return
        columns_by_table = {
            table: {column["name"] for column in inspector.get_columns(table)} for table in tables
        }
        additions = {
            "research_tasks": {"repository_id": "VARCHAR(36)", "revision_id": "VARCHAR(36)"},
            "source_documents": {"repository_id": "VARCHAR(36)", "revision_id": "VARCHAR(36)"},
            "evidence": {"repository_id": "VARCHAR(36)", "revision_id": "VARCHAR(36)"},
        }
        for table, columns in additions.items():
            if table not in tables:
                continue
            for name, sql_type in columns.items():
                if name not in columns_by_table[table]:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"))
        legacy_id = "00000000-0000-0000-0000-000000000001"
        exists = connection.execute(
            text("SELECT 1 FROM repositories WHERE id = :id"), {"id": legacy_id}
        ).first()
        if exists is None:
            connection.execute(
                text(
                    "INSERT INTO repositories "
                    "(id,name,source_type,identity_key,source_location,root_path,status,"
                    "metadata_json,created_at,updated_at) "
                    "VALUES (:id,:name,:source_type,:identity_key,:source_location,:root_path,"
                    ":status,:metadata,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                ),
                {
                    "id": legacy_id,
                    "name": "Default workspace",
                    "source_type": "local",
                    "identity_key": f"legacy:{legacy_root}",
                    "source_location": legacy_root,
                    "root_path": legacy_root,
                    "status": "ready",
                    "metadata": "{}",
                },
            )
        # A v1.3 source_documents table has a UNIQUE(source_uri, content_hash)
        # constraint.  Since that constraint cannot be altered in-place on
        # SQLite, rebuild the table once with the v1.4 composite key.  This is
        # intentionally idempotent and preserves all rows/foreign keys.
        if connection.dialect.name == "sqlite" and "source_documents" in tables:
            unique_sets = [
                tuple(item.get("column_names", ()))
                for item in inspect(connection).get_unique_constraints("source_documents")
            ]
            if ("source_uri", "content_hash") in unique_sets:
                Database._rebuild_source_documents(connection)
                inspector = inspect(connection)
                columns_by_table["source_documents"] = {
                    column["name"] for column in inspector.get_columns("source_documents")
                }

        for table in ("research_tasks", "source_documents", "evidence"):
            if table in tables and "repository_id" in columns_by_table.get(table, set()) | {
                "repository_id"
            }:
                connection.execute(
                    text(f"UPDATE {table} SET repository_id = :id WHERE repository_id IS NULL"),
                    {"id": legacy_id},
                )
        # Legacy rows need a concrete revision as NULL participates specially
        # in SQLite UNIQUE constraints (and would allow duplicate rows).
        legacy_revision_id = "00000000-0000-0000-0000-000000000002"
        if "repository_revisions" in tables:
            rev_exists = connection.execute(
                text("SELECT 1 FROM repository_revisions WHERE id = :id"),
                {"id": legacy_revision_id},
            ).first()
            if rev_exists is None:
                connection.execute(
                    text(
                        "INSERT INTO repository_revisions "
                        "(id,repository_id,revision,root_path,status,stats_json,created_at,"
                        "completed_at) VALUES (:id,:repository_id,:revision,:root_path,'ready',"
                        "'{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                    ),
                    {
                        "id": legacy_revision_id,
                        "repository_id": legacy_id,
                        "revision": "legacy",
                        "root_path": legacy_root,
                    },
                )
        for table in ("research_tasks", "source_documents", "evidence"):
            if table in tables and "revision_id" in columns_by_table.get(table, set()):
                connection.execute(
                    text(f"UPDATE {table} SET revision_id = :id WHERE revision_id IS NULL"),
                    {"id": legacy_revision_id},
                )

        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO schema_migrations(version, applied_at) "
                "SELECT 2, CURRENT_TIMESTAMP WHERE NOT EXISTS "
                "(SELECT 1 FROM schema_migrations WHERE version = 2)"
            )
        )

    @staticmethod
    def _rebuild_source_documents(connection: Connection) -> None:
        """Replace the legacy source_documents UNIQUE constraint on SQLite."""
        # Foreign-key checks must be suspended while the referenced table is
        # renamed. They are restored before returning.
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        try:
            for index in inspect(connection).get_indexes("source_documents"):
                name = index.get("name")
                if name:
                    connection.execute(text(f'DROP INDEX IF EXISTS "{name}"'))
            connection.execute(
                text("ALTER TABLE source_documents RENAME TO source_documents_legacy")
            )
            Base.metadata.tables["source_documents"].create(connection, checkfirst=False)
            columns = [
                "id",
                "repository_id",
                "revision_id",
                "source_uri",
                "source_type",
                "title",
                "content_hash",
                "version",
                "content",
                "metadata_json",
                "created_at",
            ]
            connection.execute(
                text(
                    "INSERT INTO source_documents (" + ",".join(columns) + ") "
                    "SELECT " + ",".join(columns) + " FROM source_documents_legacy"
                )
            )
            connection.execute(text("DROP TABLE source_documents_legacy"))
        finally:
            connection.execute(text("PRAGMA foreign_keys=ON"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return
        raw_path = url.removeprefix(prefix)
        if raw_path == ":memory:" or raw_path.startswith("file:"):
            return
        Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
