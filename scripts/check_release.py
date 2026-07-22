#!/usr/bin/env python3
"""Independently validate release archives, metadata, manifest, and checksums."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

FORBIDDEN_BASENAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "release-manifest.json",
    "sha256sums",
}
FORBIDDEN_NAME_TOKENS = ("api_key", "apikey", "secret")
EXTERNAL_RELEASE_RECORDS = ("docs/acceptance.md",)
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    ),
    ("aws_access_key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(rb"gh[pousr]_[A-Za-z0-9_]{30,}")),
    ("openai_style_key", re.compile(rb"sk-[A-Za-z0-9_-]{20,}")),
    ("slack_token", re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(rb"AIza[0-9A-Za-z_-]{35}")),
    ("bearer_token", re.compile(rb"Bearer [A-Za-z0-9._~-]{24,}")),
)
SECRET_PLACEHOLDER_MARKERS = (
    b"example",
    b"must-not-persist",
    b"not-a-real",
    b"placeholder",
    b"replace-with-",
    b"test-only",
)

DistributionKind = Literal["wheel", "sdist"]


def project_metadata(root: Path) -> tuple[str, str]:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project", {})
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SystemExit("pyproject.toml is missing project name/version")
    return name, version


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def canonical_project_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def normalize_member_name(name: str) -> str:
    raw = name.replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith("/") or DRIVE_PREFIX_PATTERN.match(raw):
        raise SystemExit(f"unsafe archive member path: {name!r}")
    trimmed = raw[:-1] if raw.endswith("/") else raw
    parts = trimmed.split("/")
    if not trimmed or any(part in {"", ".", ".."} for part in parts):
        raise SystemExit(f"unsafe archive member path: {name!r}")
    return "/".join(parts)


def forbidden_member(name: str) -> bool:
    normalized = name.lower()
    basename = PurePosixPath(normalized).name
    return (
        basename in FORBIDDEN_BASENAMES
        or any(normalized.endswith(record) for record in EXTERNAL_RELEASE_RECORDS)
        or any(token in basename for token in FORBIDDEN_NAME_TOKENS)
    )


def secret_pattern_labels(data: bytes) -> tuple[str, ...]:
    labels = []
    for label, pattern in SECRET_PATTERNS:
        matches = pattern.finditer(data)
        if any(
            not any(marker in match.group().lower() for marker in SECRET_PLACEHOLDER_MARKERS)
            for match in matches
        ):
            labels.append(label)
    return tuple(labels)


def read_bounded(stream: BinaryIO, expected_size: int, member_name: str) -> bytes:
    if expected_size < 0 or expected_size > MAX_MEMBER_BYTES:
        raise SystemExit(f"archive member exceeds size limit: {member_name}")
    data = stream.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES or len(data) != expected_size:
        raise SystemExit(f"invalid archive member size: {member_name}")
    return data


def validate_metadata(
    payload: bytes,
    *,
    expected_project: str,
    expected_version: str,
    artifact_name: str,
) -> None:
    metadata = BytesParser().parsebytes(payload)
    project = metadata.get("Name")
    version = metadata.get("Version")
    if not project or canonical_project_name(project) != canonical_project_name(expected_project):
        raise SystemExit(f"artifact project metadata mismatch: {artifact_name}")
    if version != expected_version:
        raise SystemExit(f"artifact version metadata mismatch: {artifact_name}")


def validate_wheel(path: Path, expected_project: str, expected_version: str) -> None:
    expected_prefix = f"{expected_project.replace('-', '_')}-{expected_version}-"
    if not path.name.startswith(expected_prefix) or not path.name.endswith(".whl"):
        raise SystemExit(f"wheel filename does not match project/version: {path.name}")

    metadata_payloads: list[bytes] = []
    seen: set[str] = set()
    total_size = 0
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if not infos:
                raise SystemExit(f"empty wheel archive: {path.name}")
            for info in infos:
                normalized = normalize_member_name(info.filename)
                if normalized in seen:
                    raise SystemExit(f"duplicate archive member: {normalized}")
                seen.add(normalized)
                if forbidden_member(normalized):
                    raise SystemExit(f"forbidden content path in {path.name}: {normalized}")
                mode = (info.external_attr >> 16) & 0o170000
                if stat.S_ISLNK(mode) or mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise SystemExit(f"unsupported wheel member type: {normalized}")
                if info.flag_bits & 0x1:
                    raise SystemExit(f"encrypted wheel member is not allowed: {normalized}")
                if info.is_dir():
                    continue
                total_size += info.file_size
                if total_size > MAX_ARCHIVE_BYTES:
                    raise SystemExit(f"wheel exceeds uncompressed size limit: {path.name}")
                with archive.open(info) as stream:
                    data = read_bounded(stream, info.file_size, normalized)
                labels = secret_pattern_labels(data)
                if labels:
                    raise SystemExit(
                        f"secret-like content in {path.name}:{normalized}: {','.join(labels)}"
                    )
                if normalized.endswith(".dist-info/METADATA"):
                    metadata_payloads.append(data)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        raise SystemExit(f"invalid wheel archive: {path.name}") from exc

    if len(metadata_payloads) != 1:
        raise SystemExit(f"wheel must contain exactly one METADATA file: {path.name}")
    validate_metadata(
        metadata_payloads[0],
        expected_project=expected_project,
        expected_version=expected_version,
        artifact_name=path.name,
    )


def validate_sdist(path: Path, expected_project: str, expected_version: str) -> None:
    expected_name = f"{expected_project.replace('-', '_')}-{expected_version}.tar.gz"
    if path.name != expected_name:
        raise SystemExit(f"sdist filename does not match project/version: {path.name}")

    metadata_payloads: list[bytes] = []
    seen: set[str] = set()
    total_size = 0
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise SystemExit(f"empty sdist archive: {path.name}")
            for member in members:
                normalized = normalize_member_name(member.name)
                if normalized in seen:
                    raise SystemExit(f"duplicate archive member: {normalized}")
                seen.add(normalized)
                if forbidden_member(normalized):
                    raise SystemExit(f"forbidden content path in {path.name}: {normalized}")
                if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                    raise SystemExit(f"unsupported sdist member type: {normalized}")
                if member.isdir():
                    continue
                total_size += member.size
                if total_size > MAX_ARCHIVE_BYTES:
                    raise SystemExit(f"sdist exceeds uncompressed size limit: {path.name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise SystemExit(f"cannot read sdist member: {normalized}")
                with stream:
                    data = read_bounded(stream, member.size, normalized)
                labels = secret_pattern_labels(data)
                if labels:
                    raise SystemExit(
                        f"secret-like content in {path.name}:{normalized}: {','.join(labels)}"
                    )
                if normalized.endswith("/PKG-INFO"):
                    metadata_payloads.append(data)
    except (OSError, tarfile.TarError) as exc:
        raise SystemExit(f"invalid sdist archive: {path.name}") from exc

    if len(metadata_payloads) != 1:
        raise SystemExit(f"sdist must contain exactly one PKG-INFO file: {path.name}")
    validate_metadata(
        metadata_payloads[0],
        expected_project=expected_project,
        expected_version=expected_version,
        artifact_name=path.name,
    )


def validate_distribution_artifact(
    path: Path, expected_project: str, expected_version: str
) -> DistributionKind:
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"distribution is not a regular file: {path.name}")
    if path.suffix == ".whl":
        validate_wheel(path, expected_project, expected_version)
        return "wheel"
    if path.name.endswith(".tar.gz"):
        validate_sdist(path, expected_project, expected_version)
        return "sdist"
    raise SystemExit(f"unsupported distribution type: {path.name}")


def validate_distribution_set(
    paths: list[Path], expected_project: str, expected_version: str
) -> None:
    if len(paths) != 2:
        raise SystemExit("release must contain exactly one wheel and one sdist")
    kinds = [
        validate_distribution_artifact(path, expected_project, expected_version) for path in paths
    ]
    if sorted(kinds) != ["sdist", "wheel"]:
        raise SystemExit("release must contain exactly one wheel and one sdist")


def load_manifest(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise SystemExit("release-manifest.json must be a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("invalid release-manifest.json") from exc
    if not isinstance(payload, dict):
        raise SystemExit("release manifest must be an object")
    return payload


def validate_release_directory(root: Path, expected_project: str, expected_version: str) -> int:
    if root.is_symlink() or not root.is_dir():
        raise SystemExit("release directory must be a regular directory")
    manifest = load_manifest(root / "release-manifest.json")
    if manifest.get("project") != expected_project or manifest.get("version") != expected_version:
        raise SystemExit("manifest project/version mismatch")
    records = manifest.get("artifacts")
    if not isinstance(records, list) or len(records) != 2:
        raise SystemExit("manifest must contain exactly one wheel and one sdist")

    lines: list[str] = []
    names: set[str] = set()
    artifacts: list[Path] = []
    for record in records:
        if not isinstance(record, dict):
            raise SystemExit("invalid manifest artifact record")
        name = record.get("name")
        expected_digest = record.get("sha256")
        expected_bytes = record.get("bytes")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or name in names
            or not (name.endswith(".whl") or name.endswith(".tar.gz"))
        ):
            raise SystemExit(f"invalid artifact name: {name!r}")
        if not isinstance(expected_digest, str) or not SHA256_PATTERN.fullmatch(expected_digest):
            raise SystemExit(f"invalid artifact digest: {name}")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 1
        ):
            raise SystemExit(f"invalid artifact size: {name}")
        names.add(name)
        path = root / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != expected_bytes
            or digest(path) != expected_digest
        ):
            raise SystemExit(f"artifact mismatch: {name}")
        artifacts.append(path)
        lines.append(f"{expected_digest}  {name}\n")

    distributions = {
        path.name
        for path in root.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    }
    if distributions != names:
        raise SystemExit("release directory distributions do not match manifest")

    checksum_path = root / "SHA256SUMS"
    if checksum_path.is_symlink() or not checksum_path.is_file():
        raise SystemExit("SHA256SUMS must be a regular file")
    if checksum_path.read_text(encoding="utf-8") != "".join(lines):
        raise SystemExit("SHA256SUMS does not match manifest")

    validate_distribution_set(artifacts, expected_project, expected_version)
    return len(artifacts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args()
    expected_project, expected_version = project_metadata(Path(__file__).resolve().parents[1])
    count = validate_release_directory(
        args.release_dir.absolute(), expected_project, expected_version
    )
    print(f"validated {count} release artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
