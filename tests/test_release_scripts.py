from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def load_script(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check_release = load_script("check_release")

PROJECT = "repo-pilot"
VERSION = "1.3.0"


def metadata(*, project: str = PROJECT, version: str = VERSION) -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        f"Name: {project}\n"
        f"Version: {version}\n"
        "Summary: release validation fixture\n\n"
    ).encode()


def write_wheel(
    path: Path,
    *,
    project: str = PROJECT,
    version: str = VERSION,
    extra_members: dict[str, bytes] | None = None,
    symlink: str | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("repo_pilot/__init__.py", b"")
        archive.writestr(
            f"repo_pilot-{VERSION}.dist-info/METADATA",
            metadata(project=project, version=version),
        )
        for name, payload in (extra_members or {}).items():
            archive.writestr(name, payload)
        if symlink is not None:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, b"target")


def add_tar_bytes(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def write_sdist(
    path: Path,
    *,
    project: str = PROJECT,
    version: str = VERSION,
    extra_members: dict[str, bytes] | None = None,
    symlink: str | None = None,
) -> None:
    root = f"repo_pilot-{VERSION}"
    with tarfile.open(path, "w:gz") as archive:
        add_tar_bytes(archive, f"{root}/PKG-INFO", metadata(project=project, version=version))
        add_tar_bytes(archive, f"{root}/pyproject.toml", b"[project]\nname='repo-pilot'\n")
        for name, payload in (extra_members or {}).items():
            add_tar_bytes(archive, name, payload)
        if symlink is not None:
            info = tarfile.TarInfo(symlink)
            info.type = tarfile.SYMTYPE
            info.linkname = "target"
            archive.addfile(info)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_release_metadata(root: Path, artifacts: list[Path]) -> None:
    records = [
        {"name": path.name, "sha256": file_digest(path), "bytes": path.stat().st_size}
        for path in artifacts
    ]
    (root / "release-manifest.json").write_text(
        json.dumps({"project": PROJECT, "version": VERSION, "artifacts": records}),
        encoding="utf-8",
    )
    (root / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['name']}\n" for record in records),
        encoding="utf-8",
    )


def valid_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    wheel = tmp_path / "repo_pilot-1.3.0-py3-none-any.whl"
    sdist = tmp_path / "repo_pilot-1.3.0.tar.gz"
    write_wheel(wheel)
    write_sdist(sdist)
    return wheel, sdist


def test_validate_release_directory_accepts_valid_archives(tmp_path: Path) -> None:
    wheel, sdist = valid_artifacts(tmp_path)
    write_release_metadata(tmp_path, [wheel, sdist])

    assert check_release.validate_release_directory(tmp_path, PROJECT, VERSION) == 2


def test_distribution_set_requires_exactly_one_wheel_and_sdist(tmp_path: Path) -> None:
    wheel, _ = valid_artifacts(tmp_path)

    with pytest.raises(SystemExit, match="exactly one wheel and one sdist"):
        check_release.validate_distribution_set([wheel], PROJECT, VERSION)


def test_rejects_non_archive_with_distribution_extension(tmp_path: Path) -> None:
    wheel = tmp_path / "repo_pilot-1.3.0-py3-none-any.whl"
    wheel.write_bytes(b"not a zip archive")

    with pytest.raises(SystemExit, match="invalid wheel archive"):
        check_release.validate_distribution_artifact(wheel, PROJECT, VERSION)


@pytest.mark.parametrize(
    ("member_name", "message"),
    [
        ("repo_pilot-1.3.0/.env", "forbidden content path"),
        ("../escape.py", "unsafe archive member path"),
    ],
)
def test_rejects_forbidden_or_unsafe_wheel_members(
    tmp_path: Path, member_name: str, message: str
) -> None:
    wheel = tmp_path / "repo_pilot-1.3.0-py3-none-any.whl"
    write_wheel(wheel, extra_members={member_name: b"fixture"})

    with pytest.raises(SystemExit, match=message):
        check_release.validate_distribution_artifact(wheel, PROJECT, VERSION)


def test_rejects_secret_like_member_content_without_echoing_secret(tmp_path: Path) -> None:
    wheel = tmp_path / "repo_pilot-1.3.0-py3-none-any.whl"
    secret = b"sk-" + b"a" * 32
    write_wheel(wheel, extra_members={"repo_pilot/config.txt": secret})

    with pytest.raises(SystemExit, match="openai_style_key") as captured:
        check_release.validate_distribution_artifact(wheel, PROJECT, VERSION)

    assert secret.decode() not in str(captured.value)


def test_rejects_wheel_symlink(tmp_path: Path) -> None:
    wheel = tmp_path / "repo_pilot-1.3.0-py3-none-any.whl"
    write_wheel(wheel, symlink="repo_pilot/link")

    with pytest.raises(SystemExit, match="unsupported wheel member type"):
        check_release.validate_distribution_artifact(wheel, PROJECT, VERSION)


def test_rejects_sdist_symlink(tmp_path: Path) -> None:
    sdist = tmp_path / "repo_pilot-1.3.0.tar.gz"
    write_sdist(sdist, symlink="repo_pilot-1.3.0/link")

    with pytest.raises(SystemExit, match="unsupported sdist member type"):
        check_release.validate_distribution_artifact(sdist, PROJECT, VERSION)


@pytest.mark.parametrize(("project", "version"), [("other-project", VERSION), (PROJECT, "9.9.9")])
def test_rejects_wheel_metadata_mismatch(tmp_path: Path, project: str, version: str) -> None:
    wheel = tmp_path / "repo_pilot-1.3.0-py3-none-any.whl"
    write_wheel(wheel, project=project, version=version)

    with pytest.raises(SystemExit, match="metadata mismatch"):
        check_release.validate_distribution_artifact(wheel, PROJECT, VERSION)


def test_release_directory_rejects_unlisted_distribution(tmp_path: Path) -> None:
    wheel, sdist = valid_artifacts(tmp_path)
    write_release_metadata(tmp_path, [wheel, sdist])
    (tmp_path / "unexpected.whl").write_bytes(b"extra")

    with pytest.raises(SystemExit, match="do not match manifest"):
        check_release.validate_release_directory(tmp_path, PROJECT, VERSION)


def test_secret_patterns_report_labels_not_values() -> None:
    secret = b"ghp_" + b"b" * 36

    assert check_release.secret_pattern_labels(secret) == ("github_token",)


def test_secret_patterns_allow_explicit_non_secret_fixture_marker() -> None:
    fixture = b"sk-must-not-persist-" + b"c" * 32

    assert check_release.secret_pattern_labels(fixture) == ()
