#!/usr/bin/env python3
"""Create a deterministic recursive corresponding-source release."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Any


SUPERPROJECT_BASELINE = "ad8b2d470569da6826ecad812209cc6c8787f7cf"
PATCH_BASELINES = {
    ".": SUPERPROJECT_BASELINE,
    "core": "2b23d1c30cb39ba0202a9b86a752388a6b13f52a",
    "web-apps": "91f0f979fdecda9d0cf416cef63ba8de9e990624",
}
class ReleaseError(RuntimeError):
    """The checked-out source cannot produce an exact release."""


def run(
    *command: str,
    cwd: Path | None = None,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )


def git(repo: Path, *arguments: str, check: bool = True) -> str:
    result = run("git", *arguments, cwd=repo, check=check)
    assert isinstance(result.stdout, str)
    return result.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: str) -> Path:
    pure = PurePosixPath(value)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ReleaseError(f"unsafe submodule path: {value!r}")
    return Path(*pure.parts)


def submodule_paths(repo: Path) -> list[str]:
    modules = repo / ".gitmodules"
    if not modules.is_file():
        return []
    result = run(
        "git",
        "config",
        "--file",
        str(modules),
        "--get-regexp",
        r"^submodule\..*\.path$",
        cwd=repo,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ReleaseError(result.stderr.strip())
    assert isinstance(result.stdout, str)
    paths: list[str] = []
    for line in result.stdout.splitlines():
        _, value = line.split(None, 1)
        paths.append(value)
    return paths


def configured_submodule_url(repo: Path, configured_path: str) -> str:
    """Return the canonical URL recorded by the parent superproject.

    A checkout's local ``origin`` is operator state: it may still name an
    upstream repository after the superproject has deliberately moved a
    gitlink to a fork.  Corresponding-source metadata must instead describe
    the immutable checkout coordinates encoded in the parent's
    ``.gitmodules`` file.
    """

    modules = repo / ".gitmodules"
    result = run(
        "git",
        "config",
        "--file",
        str(modules),
        "--get-regexp",
        r"^submodule\..*\.path$",
        cwd=repo,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ReleaseError(result.stderr.strip())
    assert isinstance(result.stdout, str)
    for line in result.stdout.splitlines():
        key, value = line.split(None, 1)
        if value != configured_path:
            continue
        url_key = f"{key[:-len('.path')]}.url"
        url_result = run(
            "git",
            "config",
            "--file",
            str(modules),
            "--get",
            url_key,
            cwd=repo,
            check=False,
        )
        if url_result.returncode != 0:
            raise ReleaseError(
                f"submodule URL is missing for configured path: {configured_path}"
            )
        assert isinstance(url_result.stdout, str)
        url = url_result.stdout.strip()
        if not url:
            raise ReleaseError(
                f"submodule URL is empty for configured path: {configured_path}"
            )
        return url
    raise ReleaseError(f"submodule path is not configured: {configured_path}")


def collect_submodules(root: Path) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []

    def walk(repo: Path, prefix: Path) -> None:
        for configured in submodule_paths(repo):
            relative = safe_relative(configured)
            display = prefix / relative
            checkout = root / display
            if not checkout.is_dir():
                raise ReleaseError(f"submodule checkout is missing: {display.as_posix()}")
            recorded_line = git(repo, "ls-tree", "HEAD", "--", configured)
            fields = recorded_line.split()
            if len(fields) < 3 or fields[1] != "commit":
                raise ReleaseError(f"path is not a recorded gitlink: {display.as_posix()}")
            recorded = fields[2]
            actual = git(checkout, "rev-parse", "HEAD")
            if actual != recorded:
                raise ReleaseError(
                    f"submodule {display.as_posix()} is at {actual}, expected {recorded}"
                )
            status = git(checkout, "status", "--porcelain", "--untracked-files=all")
            if status:
                raise ReleaseError(f"submodule is dirty: {display.as_posix()}")
            collected.append(
                {
                    "path": display.as_posix(),
                    "commit": actual,
                    "remote": configured_submodule_url(repo, configured),
                }
            )
            walk(checkout, display)

    walk(root, Path())
    return collected


def extract_archive(repo: Path, revision: str, destination: Path, *, prefix: str = "") -> None:
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", f"--prefix={prefix}", revision],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    extract = subprocess.run(
        ["tar", "-xf", "-", "-C", str(destination)],
        stdin=archive.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    archive.stdout.close()
    archive_stderr = archive.communicate()[1]
    if archive.returncode or extract.returncode:
        message = b"\n".join(part for part in (archive_stderr, extract.stderr) if part)
        raise ReleaseError(message.decode("utf-8", errors="replace"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def generate_patch_series(
    root: Path, source_root: Path, submodules: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    commits = {".": git(root, "rev-parse", "HEAD")}
    commits.update({item["path"]: item["commit"] for item in submodules})
    result: list[dict[str, Any]] = []
    patch_root = source_root / "NEOHARNESS-RELEASE" / "patches"
    for path, baseline in PATCH_BASELINES.items():
        repo = root if path == "." else root / path
        head = commits.get(path)
        if head is None or head == baseline:
            continue
        ancestor = run(
            "git",
            "merge-base",
            "--is-ancestor",
            baseline,
            head,
            cwd=repo,
            check=False,
        )
        if ancestor.returncode != 0:
            raise ReleaseError(f"patch baseline is not an ancestor for {path}: {baseline}")
        label = "superproject" if path == "." else path
        destination = patch_root / label
        destination.mkdir(parents=True, exist_ok=True)
        completed = run(
            "git",
            "format-patch",
            "--no-signature",
            "--output-directory",
            str(destination),
            f"{baseline}..{head}",
            cwd=repo,
        )
        assert isinstance(completed.stdout, str)
        patches = sorted(item.name for item in destination.glob("*.patch"))
        result.append(
            {
                "repository_path": path,
                "baseline": baseline,
                "head": head,
                "patches": patches,
            }
        )
    return result


def license_inventory(source_root: Path) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for path in source_root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if not (
            name.startswith(
                ("license", "copying", "notice", "ofl", "gpl", "lgpl", "agpl")
            )
            or name.endswith(("-license", "-license.txt", "-license.md"))
            or name in {"copyright", "copyright.txt"}
            or "third_party_notice" in name
            or "third-party-notice" in name
        ):
            continue
        relative = path.relative_to(source_root).as_posix()
        result.append(
            {"path": relative, "size": path.stat().st_size, "sha256": sha256(path)}
        )
    result.sort(key=lambda item: str(item["path"]))
    return result


def deterministic_tar(source_parent: Path, name: str, output: Path, epoch: int) -> None:
    tar = subprocess.Popen(
        [
            "tar",
            "--sort=name",
            f"--mtime=@{epoch}",
            "--owner=0",
            "--group=0",
            "--numeric-owner",
            "--pax-option=delete=atime,delete=ctime",
            "-C",
            str(source_parent),
            "-cf",
            "-",
            name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert tar.stdout is not None
    zstd = subprocess.run(
        ["zstd", "-19", "-T0", "-f", "-o", str(output)],
        stdin=tar.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    tar.stdout.close()
    tar_stderr = tar.communicate()[1]
    if tar.returncode or zstd.returncode:
        message = b"\n".join(part for part in (tar_stderr, zstd.stderr) if part)
        raise ReleaseError(message.decode("utf-8", errors="replace"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--version", default="9.3.3-nh1")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    root = Path(git(arguments.repo.resolve(), "rev-parse", "--show-toplevel"))
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("superproject is dirty")
    head = git(root, "rev-parse", "HEAD")
    if run(
        "git",
        "merge-base",
        "--is-ancestor",
        SUPERPROJECT_BASELINE,
        head,
        cwd=root,
        check=False,
    ).returncode != 0:
        raise ReleaseError("the v9.3.3 source baseline is not an ancestor of HEAD")
    epoch = int(git(root, "show", "-s", "--format=%ct", "HEAD"))
    submodules = collect_submodules(root)

    output = arguments.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ReleaseError(f"refusing to replace existing release: {output}")

    bundle_name = f"neoharness-office-{arguments.version}-source"
    with tempfile.TemporaryDirectory(prefix="nho-source-release-", dir=output.parent) as raw:
        temporary = Path(raw)
        source_root = temporary / bundle_name
        extract_archive(root, head, temporary, prefix=f"{bundle_name}/")
        for item in sorted(submodules, key=lambda value: value["path"].count("/")):
            relative = safe_relative(str(item["path"]))
            destination = source_root / relative
            shutil.rmtree(destination, ignore_errors=True)
            destination.mkdir(parents=True, exist_ok=True)
            extract_archive(root / relative, str(item["commit"]), destination)

        release_root = source_root / "NEOHARNESS-RELEASE"
        patch_series = generate_patch_series(root, source_root, submodules)
        manifest = {
            "schema": "ai.neoharness.office.source-release.v1",
            "version": arguments.version,
            "source_commit": head,
            "source_remote": remote_url(root),
            "source_date_epoch": epoch,
            "upstream_baseline": SUPERPROJECT_BASELINE,
            "submodules": submodules,
            "patch_series": patch_series,
        }
        write_json(release_root / "SOURCE-MANIFEST.json", manifest)
        write_json(
            release_root / "LICENSE-FILES.json", license_inventory(source_root)
        )
        deterministic_tar(temporary, bundle_name, output, epoch)

    sidecar = output.with_suffix(output.suffix + ".json")
    write_json(
        sidecar,
        {
            **manifest,
            "archive": {
                "name": output.name,
                "size": output.stat().st_size,
                "sha256": sha256(output),
            },
        },
    )
    print(json.dumps({"archive": str(output), "sha256": sha256(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
