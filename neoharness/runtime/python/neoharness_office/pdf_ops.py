from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from .paths import file_fact
from .process import UtilityError, run_utility
from .quality import artifact_provenance


PAGE_SPEC = re.compile(r"^[0-9zZ,._+\-]+$")


def _temporary_sibling(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def _pdf_result(
    operation: str,
    inputs: list[Path],
    output: Path,
    execution: dict[str, object],
) -> dict[str, object]:
    return {
        "operation": operation,
        "inputs": [file_fact(path) for path in inputs],
        "output": file_fact(output, mime_type="application/pdf"),
        "provenance": artifact_provenance(
            output,
            finalizer="pdf_page",
            producer="qpdf",
            operation=operation,
        ),
        "execution": {
            "command": execution["command"],
            "returncode": execution["returncode"],
            "duration_ms": execution["duration_ms"],
            "stdout_bytes": execution["stdout_bytes"],
            "stderr_bytes": execution["stderr_bytes"],
        },
    }


def _materialize(
    operation: str,
    inputs: list[Path],
    output: Path,
    command: list[str],
    *,
    timeout: float = 300,
) -> dict[str, object]:
    temporary = _temporary_sibling(output)
    expanded = [str(temporary) if item == "{output}" else item for item in command]
    try:
        execution = run_utility(expanded, timeout=timeout)
        if not temporary.is_file() or temporary.is_symlink():
            raise UtilityError(f"{operation} did not produce a regular PDF")
        validation = run_utility(["qpdf", "--check", str(temporary)], timeout=60)
        os.replace(temporary, output)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    result = _pdf_result(operation, inputs, output, execution)
    result["validation"] = {
        "command": validation["command"],
        "returncode": validation["returncode"],
        "duration_ms": validation["duration_ms"],
    }
    return result


def merge(inputs: list[Path], output: Path) -> dict[str, object]:
    if len(inputs) < 2:
        raise ValueError("PDF merge requires at least two inputs")
    return _materialize(
        "merge_pdf",
        inputs,
        output,
        [
            "qpdf",
            "--empty",
            "--pages",
            *[str(path) for path in inputs],
            "--",
            "{output}",
        ],
    )


def extract(source: Path, output: Path, *, pages: str) -> dict[str, object]:
    if not pages or PAGE_SPEC.fullmatch(pages) is None:
        raise ValueError("page selection contains unsupported qpdf page syntax")
    return _materialize(
        "extract_pdf_pages",
        [source],
        output,
        ["qpdf", str(source), "--pages", ".", pages, "--", "{output}"],
    )


def rotate(
    source: Path,
    output: Path,
    *,
    degrees: int,
    pages: str = "1-z",
) -> dict[str, object]:
    if degrees not in {90, 180, 270, -90, -180, -270}:
        raise ValueError("rotation must be ±90, ±180, or ±270 degrees")
    if PAGE_SPEC.fullmatch(pages) is None:
        raise ValueError("page selection contains unsupported qpdf page syntax")
    sign = "+" if degrees > 0 else ""
    return _materialize(
        "rotate_pdf_pages",
        [source],
        output,
        [
            "qpdf",
            str(source),
            f"--rotate={sign}{degrees}:{pages}",
            "--",
            "{output}",
        ],
    )


def linearize(source: Path, output: Path) -> dict[str, object]:
    return _materialize(
        "linearize_pdf",
        [source],
        output,
        ["qpdf", "--linearize", str(source), "{output}"],
    )
