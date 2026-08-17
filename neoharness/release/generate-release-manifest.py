#!/usr/bin/env python3
"""Record exact release identities and artifact digests."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


def run(*command: str) -> str:
    return subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_identity(reference: str) -> dict[str, Any]:
    raw = json.loads(run("docker", "image", "inspect", reference))[0]
    config = raw.get("Config") or {}
    return {
        "reference": reference,
        "id": raw["Id"],
        "repo_digests": sorted(raw.get("RepoDigests") or []),
        "architecture": raw["Architecture"],
        "os": raw["Os"],
        "created": raw["Created"],
        "labels": dict(sorted((config.get("Labels") or {}).items())),
    }


def file_inventory(root: Path) -> list[dict[str, Any]]:
    ignored = {"RELEASE-MANIFEST.json", "SHA256SUMS"}
    result: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.relative_to(root).as_posix() in ignored:
            continue
        result.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-date-epoch", type=int, required=True)
    parser.add_argument("--viewer-image", required=True)
    parser.add_argument("--runtime-image", required=True)
    parser.add_argument("--viewer-font-digest", required=True)
    parser.add_argument("--runtime-font-digest", required=True)
    arguments = parser.parse_args()

    root = arguments.release_root.resolve()
    manifest = {
        "schema": "ai.neoharness.office.release.v1",
        "version": arguments.version,
        "source_commit": arguments.source_commit,
        "source_date_epoch": arguments.source_date_epoch,
        "images": {
            "viewer": image_identity(arguments.viewer_image),
            "runtime": image_identity(arguments.runtime_image),
        },
        "font_payload": {
            "viewer_sha256": arguments.viewer_font_digest,
            "runtime_sha256": arguments.runtime_font_digest,
            "identical": arguments.viewer_font_digest
            == arguments.runtime_font_digest,
        },
        "artifacts": file_inventory(root),
    }
    if not manifest["font_payload"]["identical"]:
        raise SystemExit("viewer and runtime font payloads differ")
    output = root / "RELEASE-MANIFEST.json"
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    checksum_paths = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "SHA256SUMS"
    ]
    with (root / "SHA256SUMS").open("w", encoding="utf-8") as destination:
        for path in checksum_paths:
            destination.write(
                f"{sha256(path)}  {path.relative_to(root).as_posix()}\n"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
