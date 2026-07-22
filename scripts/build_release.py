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
import tempfile
import tomllib
from pathlib import Path

if __package__:
    from .check_release import validate_distribution_set
else:
    from check_release import validate_distribution_set


def project_metadata(root: Path) -> tuple[str, str]:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project", {})
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise SystemExit("pyproject.toml is missing project name/version")
    return name, version


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_distribution(path: Path) -> bool:
    return path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=Path("release"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project_name, project_version = project_metadata(root)
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="repopilot-build-") as temporary:
        build_dir = Path(temporary) / "dist"
        subprocess.run(["uv", "build", "--out-dir", str(build_dir)], cwd=root, check=True)
        artifacts = sorted(path for path in build_dir.iterdir() if is_distribution(path))
        validate_distribution_set(artifacts, project_name, project_version)
        copied_artifacts = []
        for artifact in artifacts:
            destination = out_dir / artifact.name
            shutil.copy2(artifact, destination)
            copied_artifacts.append(destination)

    records = [
        {"name": artifact.name, "sha256": sha256(artifact), "bytes": artifact.stat().st_size}
        for artifact in copied_artifacts
    ]
    manifest = {"project": project_name, "version": project_version, "artifacts": records}
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
