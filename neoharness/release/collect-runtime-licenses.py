#!/usr/bin/env python3
"""Collect license texts for source trees embedded in the headless runtime."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


RUNTIME_SOURCE_ROOTS = (
    "core",
    "sdkjs",
    "core-fonts",
    "dictionaries",
    "document-templates",
    "web-apps/vendor/xregexp",
    "neoharness/release/font-licenses",
)


def is_license(path: Path) -> bool:
    name = path.name.lower()
    return (
        name.startswith(("license", "copying", "notice", "ofl", "gpl", "lgpl", "agpl"))
        or name.endswith(("-license", "-license.txt", "-license.md"))
        or name in {"copyright", "copyright.txt"}
    )


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()

    source = arguments.source.resolve()
    destination = arguments.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    inventory: list[dict[str, object]] = []

    for relative_root in RUNTIME_SOURCE_ROOTS:
        root = source / relative_root
        if not root.is_dir():
            raise SystemExit(f"required runtime source tree is missing: {relative_root}")
        for path in sorted(root.rglob("*")):
            if not path.is_file() or not is_license(path):
                continue
            relative = path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            inventory.append(
                {
                    "path": relative.as_posix(),
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                }
            )

    if not inventory:
        raise SystemExit("no runtime license texts were discovered")
    (destination / "LICENSE-FILES.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
