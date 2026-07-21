#!/usr/bin/env python3
"""Build reproducible release artifacts and an external, non-self-referential manifest.

The manifest and checksum file live beside (not inside) the wheel/sdist, so their
hashes never become inputs to the artifact bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

FORBIDDEN_BASENAMES = {
    ".env",
    "id_rsa",
    "id_ed25519",
    "release-manifest.json",
    "sha256sums",
}
FORBIDDEN_NAME_TOKENS = ("api_key", "apikey", "secret")
EXTERNAL_RELEASE_RECORDS = ("docs/acceptance.md",)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def members(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def is_distribution(path: Path) -> bool:
    return path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))


def forbidden_member(name: str) -> bool:
    normalized = name.replace("\\", "/").lower().lstrip("./")
    basename = Path(normalized).name
    return (
        basename in FORBIDDEN_BASENAMES
        or any(normalized.endswith(record) for record in EXTERNAL_RELEASE_RECORDS)
        or any(token in basename for token in FORBIDDEN_NAME_TOKENS)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("release"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="repopilot-build-") as temporary:
        build_dir = Path(temporary) / "dist"
        subprocess.run(["uv", "build", "--out-dir", str(build_dir)], cwd=root, check=True)
        artifacts = sorted(path for path in build_dir.iterdir() if is_distribution(path))
        if not artifacts:
            raise SystemExit("uv build produced no artifacts")
        copied_artifacts = []
        for artifact in artifacts:
            names = members(artifact)
            bad = [name for name in names if forbidden_member(name)]
            if bad:
                raise SystemExit(f"forbidden content in {artifact.name}: {bad}")
            destination = out_dir / artifact.name
            shutil.copy2(artifact, destination)
            copied_artifacts.append(destination)

    records = [
        {"name": artifact.name, "sha256": sha256(artifact), "bytes": artifact.stat().st_size}
        for artifact in copied_artifacts
    ]
    manifest = {"project": "repo-pilot", "version": "1.1.0", "artifacts": records}
    (out_dir / "release-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "SHA256SUMS").write_text(
        "".join(f"{record['sha256']}  {record['name']}\n" for record in records),
        encoding="utf-8",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
