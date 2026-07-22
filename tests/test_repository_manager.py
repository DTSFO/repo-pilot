from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from repopilot import repository_manager as manager_module
from repopilot.config import Settings
from repopilot.repository_manager import (
    RepositoryManager,
    RepositoryRequestError,
    RepositorySyncError,
)
from repopilot.storage.database import Database
from repopilot.storage.repositories import DocumentStore


def manager_settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "provider": "deterministic",
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/repositories.db",
            "workspace_root": str(tmp_path),
            "repository_root": str(tmp_path / "clones"),
            "allowed_repository_roots": str(tmp_path),
            "repository_sync_timeout_seconds": 1,
        }
    )


async def test_refresh_failure_preserves_and_retry_replaces_ready_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    source = root / "README.md"
    source.write_text("first revision", encoding="utf-8")
    settings = manager_settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(tmp_path))
    manager = RepositoryManager(database, settings)
    try:
        repository = await manager.add_local(str(root))
        first = await manager.refresh(repository.id)
        source.write_text("second revision", encoding="utf-8")
        second_hash = await manager._revision(root)

        async def fail_ingestion(
            ingestor: manager_module.RepositoryIngestor,
            *args: object,
            **kwargs: object,
        ) -> object:
            del args, kwargs
            await ingestor.documents.upsert_document(
                source_uri="README.md",
                source_type="repository_file",
                title="README.md",
                content="second revision",
            )
            raise RuntimeError("synthetic indexing failure")

        original = manager_module.RepositoryIngestor.ingest_path
        monkeypatch.setattr(manager_module.RepositoryIngestor, "ingest_path", fail_ingestion)
        with pytest.raises(RepositorySyncError):
            await manager.refresh(repository.id)

        after_failure = await manager.store.get_repository(repository.id)
        assert after_failure is not None
        assert after_failure.status == "ready"
        assert after_failure.indexed_revision_id == first.id
        assert after_failure.last_error == "repository_index_failed"
        failed = await manager.store.create_revision(
            repository.id, revision=second_hash, root_path=str(root)
        )
        assert failed.status == "failed"

        monkeypatch.setattr(manager_module.RepositoryIngestor, "ingest_path", original)
        second = await manager.refresh(repository.id)
        after_retry = await manager.store.get_repository(repository.id)
        assert second.id == failed.id
        assert second.status == "ready"
        assert after_retry is not None
        assert after_retry.indexed_revision_id == second.id
        assert after_retry.last_error is None
        assert await DocumentStore(database, repository.id, second.id).latest_chunk_rows()
    finally:
        await database.close()


async def test_ready_revision_refresh_restores_stale_repository_pointer(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    (root / "README.md").write_text("stable", encoding="utf-8")
    settings = manager_settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(tmp_path))
    manager = RepositoryManager(database, settings)
    try:
        repository = await manager.add_local(str(root))
        ready = await manager.refresh(repository.id)
        await manager.store.update_repository(
            repository.id,
            status="failed",
            indexed_revision_id=None,
            last_error="stale_state",
        )

        restored = await manager.refresh(repository.id)
        current = await manager.store.get_repository(repository.id)
        assert restored.id == ready.id
        assert current is not None
        assert current.status == "ready"
        assert current.indexed_revision_id == ready.id
        assert current.last_error is None
    finally:
        await database.close()


async def test_git_validation_rejects_credentials_private_hosts_and_ports(
    tmp_path: Path,
) -> None:
    settings = manager_settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(tmp_path))
    manager = RepositoryManager(database, settings)
    try:
        rejected = (
            "http://example.com/org/repo.git",
            "https://user:secret@example.com/org/repo.git",
            "https://127.0.0.1/org/repo.git",
            "https://10.0.0.1/org/repo.git",
            "https://example.com:8443/org/repo.git",
            "file:///tmp/repo",
        )
        for url in rejected:
            with pytest.raises(RepositoryRequestError):
                await manager.add_git(url)
    finally:
        await database.close()


async def test_git_clone_target_is_name_independent_and_duplicate_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = manager_settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(tmp_path))
    manager = RepositoryManager(database, settings)
    calls: list[tuple[str, ...]] = []

    class SuccessfulProcess:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

        def kill(self) -> None:
            return None

    async def fake_subprocess(*arguments: str, **kwargs: object) -> SuccessfulProcess:
        del kwargs
        calls.append(arguments)
        return SuccessfulProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess)
    try:
        url = "https://93.184.216.34/org/repo.git"
        first = await manager.add_git(url, name="../../escape")
        second = await manager.add_git(url, name="ignored duplicate")

        root_path = Path(first.root_path)
        assert first.id == second.id
        assert root_path.parent == settings.resolved_repository_root
        assert "escape" not in root_path.parts
        assert len(calls) == 1
    finally:
        await database.close()


async def test_failed_git_clone_removes_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = manager_settings(tmp_path)
    database = Database(settings.database_url)
    await database.initialize(legacy_root=str(tmp_path))
    manager = RepositoryManager(database, settings)

    class FailedProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"clone failed"

        def kill(self) -> None:
            return None

    async def fake_subprocess(*arguments: str, **kwargs: object) -> FailedProcess:
        del kwargs
        await asyncio.to_thread(Path(arguments[-1]).mkdir, parents=True)
        return FailedProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_subprocess)
    try:
        with pytest.raises(RepositorySyncError):
            await manager.add_git("https://93.184.216.34/org/repo.git")
        clone_root = settings.resolved_repository_root
        assert clone_root.exists()
        assert list(clone_root.iterdir()) == []
    finally:
        await database.close()
