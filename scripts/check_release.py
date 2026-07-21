#!/usr/bin/env python3
"""Validate release artifacts and their external manifest/checksum file."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

EXPECTED_PROJECT = "repo-pilot"
EXPECTED_VERSION = "1.1.0"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir", type=Path)
    args = parser.parse_args()
    root = args.release_dir.resolve()
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    if manifest.get("project") != EXPECTED_PROJECT or manifest.get("version") != EXPECTED_VERSION:
        raise SystemExit("manifest project/version mismatch")
    records = manifest.get("artifacts", [])
    if not isinstance(records, list) or not records:
        raise SystemExit("manifest contains no artifacts")
    lines = []
    names: set[str] = set()
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
            not path.is_file()
            or path.stat().st_size != expected_bytes
            or digest(path) != expected_digest
        ):
            raise SystemExit(f"artifact mismatch: {name}")
        lines.append(f"{expected_digest}  {name}\n")
    distributions = {
        path.name
        for path in root.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    }
    if distributions != names:
        raise SystemExit("release directory distributions do not match manifest")
    if (root / "SHA256SUMS").read_text(encoding="utf-8") != "".join(lines):
        raise SystemExit("SHA256SUMS does not match manifest")
    print(f"validated {len(records)} release artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
