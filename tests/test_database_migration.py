from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import inspect, text

from repopilot.storage import Database
from repopilot.storage.models import LEGACY_REPOSITORY_ID

LEGACY_REVISION_ID = "00000000-0000-0000-0000-000000000002"


class DatabaseMigrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_v13_sqlite_upgrade_is_scoped_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            self._create_v13_database(path)
            database = Database(f"sqlite+aiosqlite:///{path}")
            try:
                await database.initialize(legacy_root=directory)
                await database.initialize(legacy_root=directory)

                async with database.engine.begin() as connection:
                    constraints = await connection.run_sync(
                        lambda sync: inspect(sync).get_unique_constraints("source_documents")
                    )
                    rows = (
                        await connection.execute(
                            text(
                                "SELECT repository_id, revision_id FROM source_documents "
                                "WHERE id = 'doc-1'"
                            )
                        )
                    ).one()
                    migrations = (
                        await connection.execute(
                            text("SELECT COUNT(*) FROM schema_migrations WHERE version = 2")
                        )
                    ).scalar_one()
                    revisions = (
                        await connection.execute(
                            text("SELECT COUNT(*) FROM repository_revisions WHERE id = :id"),
                            {"id": LEGACY_REVISION_ID},
                        )
                    ).scalar_one()

                    # The same URI/hash is legal in a different repository/revision.
                    await connection.execute(
                        text(
                            "INSERT INTO repositories "
                            "(id,name,source_type,identity_key,source_location,root_path,"
                            "status,metadata_json,created_at,updated_at) VALUES "
                            "('repo-2','Second','local','second','/tmp/second','/tmp/second',"
                            "'ready','{}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
                        )
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO source_documents "
                            "(id,repository_id,revision_id,source_uri,source_type,title,"
                            "content_hash,version,content,metadata_json,created_at) VALUES "
                            "('doc-2','repo-2','rev-2','README.md','file','README','hash',"
                            "1,'two','{}',CURRENT_TIMESTAMP)"
                        )
                    )

                unique_sets = {tuple(item["column_names"]) for item in constraints}
                self.assertNotIn(("source_uri", "content_hash"), unique_sets)
                self.assertIn(
                    ("repository_id", "revision_id", "source_uri", "content_hash"),
                    unique_sets,
                )
                self.assertEqual(rows, (LEGACY_REPOSITORY_ID, LEGACY_REVISION_ID))
                self.assertEqual(migrations, 1)
                self.assertEqual(revisions, 1)
            finally:
                await database.close()

    @staticmethod
    def _create_v13_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE research_tasks (
                    id VARCHAR(36) PRIMARY KEY, goal TEXT NOT NULL,
                    constraints_json JSON NOT NULL, budget_json JSON NOT NULL,
                    status VARCHAR(32) NOT NULL, current_node VARCHAR(32), final_report TEXT,
                    error_code VARCHAR(64), degraded BOOLEAN NOT NULL, version INTEGER NOT NULL,
                    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
                );
                CREATE TABLE source_documents (
                    id VARCHAR(36) PRIMARY KEY, source_uri TEXT NOT NULL,
                    source_type VARCHAR(32) NOT NULL, title TEXT NOT NULL,
                    content_hash VARCHAR(64) NOT NULL, version INTEGER NOT NULL,
                    content TEXT NOT NULL, metadata_json JSON NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_source_version UNIQUE (source_uri, content_hash)
                );
                CREATE TABLE evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id VARCHAR(36) NOT NULL,
                    document_id VARCHAR(36), source_uri TEXT NOT NULL, title TEXT NOT NULL,
                    snippet TEXT NOT NULL, score FLOAT NOT NULL, metadata_json JSON NOT NULL,
                    created_at DATETIME NOT NULL
                );
                INSERT INTO research_tasks VALUES
                    ('task-1','goal','{}','{}','completed',NULL,'report',NULL,0,1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
                INSERT INTO source_documents VALUES
                    ('doc-1','README.md','file','README','hash',1,'one','{}',CURRENT_TIMESTAMP);
                INSERT INTO evidence
                    (task_id,document_id,source_uri,title,snippet,score,metadata_json,created_at)
                    VALUES ('task-1','doc-1','README.md','README','snippet',1.0,'{}',
                            CURRENT_TIMESTAMP);
                """
            )
            connection.commit()
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
