from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile

from .paths import file_fact
from .process import UtilityError, run_utility
from .quality import artifact_provenance


LANGUAGES = re.compile(r"^[A-Za-z0-9_+-]{2,128}$")


def _temporary_sibling(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def make_searchable_pdf(
    source: Path,
    output: Path,
    *,
    languages: str = "eng",
    force: bool = False,
    deskew: bool = True,
    rotate_pages: bool = True,
) -> dict[str, object]:
    if output.suffix.lower() != ".pdf":
        raise ValueError("OCR output must use a .pdf extension")
    if LANGUAGES.fullmatch(languages) is None:
        raise ValueError("OCR languages must be a plus-separated Tesseract language list")
    temporary = _temporary_sibling(output)
    try:
        if source.suffix.lower() == ".pdf":
            command = [
                "ocrmypdf",
                "--output-type",
                "pdf",
                "--optimize",
                "1",
                "--language",
                languages,
            ]
            command.append("--force-ocr" if force else "--skip-text")
            if deskew:
                command.append("--deskew")
            if rotate_pages:
                command.append("--rotate-pages")
            command.extend([str(source), str(temporary)])
            execution = run_utility(command, timeout=900)
        else:
            base = temporary.with_suffix("")
            execution = run_utility(
                [
                    "tesseract",
                    str(source),
                    str(base),
                    "-l",
                    languages,
                    "pdf",
                ],
                timeout=900,
            )
            produced = Path(f"{base}.pdf")
            if produced != temporary:
                if not produced.is_file() or produced.is_symlink():
                    raise UtilityError("Tesseract did not produce a regular PDF")
                os.replace(produced, temporary)
        if not temporary.is_file() or temporary.is_symlink():
            raise UtilityError("OCR did not produce a regular PDF")
        validation = run_utility(["qpdf", "--check", str(temporary)], timeout=60)
        text = run_utility(
            ["pdftotext", str(temporary), "-"], timeout=120, check=False
        )
        os.replace(temporary, output)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    visible_characters = len(re.sub(r"\s+", "", str(text["stdout"])))
    producer = Path(str(execution["command"][0])).name
    return {
        "operation": "make_searchable_pdf",
        "source": file_fact(source),
        "output": file_fact(output, mime_type="application/pdf"),
        "provenance": artifact_provenance(
            output,
            finalizer="pdf_ocr",
            producer=producer,
            operation="make_searchable_pdf",
        ),
        "languages": languages,
        "force": force,
        "deskew": deskew,
        "rotate_pages": rotate_pages,
        "sampled_visible_characters": visible_characters,
        "execution": {
            "command": execution["command"],
            "returncode": execution["returncode"],
            "duration_ms": execution["duration_ms"],
            "stdout_bytes": execution["stdout_bytes"],
            "stderr_bytes": execution["stderr_bytes"],
        },
        "validation": {
            "command": validation["command"],
            "returncode": validation["returncode"],
            "duration_ms": validation["duration_ms"],
        },
    }
