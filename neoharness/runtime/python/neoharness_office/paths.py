from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
import stat


WORKSPACE_ROOT = Path("/workspace")
INPUT_ROOT = WORKSPACE_ROOT / "input"
WORK_ROOT = WORKSPACE_ROOT / "work"
OUTPUT_ROOT = WORKSPACE_ROOT / "output"
READ_ROOTS = (INPUT_ROOT, WORK_ROOT, OUTPUT_ROOT)
WRITE_ROOTS = (WORK_ROOT, OUTPUT_ROOT)


class WorkspaceContractError(ValueError):
    """A local helper was asked to cross the exact workspace boundary."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _inside_any(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(_inside(path, root.resolve(strict=True)) for root in roots)


def exact_read_file(raw: str | Path, *, roots: tuple[Path, ...] = READ_ROOTS) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise WorkspaceContractError("file path must be absolute beneath /workspace")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceContractError(f"file does not exist: {path}") from exc
    if not _inside_any(resolved, roots):
        raise WorkspaceContractError(f"file must remain beneath /workspace: {path}")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceContractError(f"file must be regular and not a symlink: {path}")
    return resolved


def exact_write_file(raw: str | Path, *, roots: tuple[Path, ...] = WRITE_ROOTS) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise WorkspaceContractError("output path must be absolute beneath /workspace")
    try:
        parent = path.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise WorkspaceContractError(f"output directory does not exist: {path.parent}") from exc
    resolved = parent / path.name
    if not _inside_any(resolved, roots):
        raise WorkspaceContractError(f"output must remain beneath /workspace: {path}")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise WorkspaceContractError(
                f"output may only replace a regular no-follow file: {path}"
            )
    return resolved


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guessed_mime(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def file_fact(path: Path, *, mime_type: str | None = None) -> dict[str, object]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise WorkspaceContractError(f"artifact is not a regular no-follow file: {path}")
    return {
        "path": str(path),
        "name": path.name,
        "size": info.st_size,
        "sha256": sha256_file(path),
        "mime_type": mime_type or guessed_mime(path),
    }
