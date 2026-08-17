from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

from .paths import file_fact
from .process import UtilityError, run_utility
from .quality import artifact_provenance


OOXML_EXTENSIONS = {
    ".docx",
    ".docm",
    ".dotx",
    ".dotm",
    ".xlsx",
    ".xlsm",
    ".xltx",
    ".xltm",
    ".pptx",
    ".pptm",
    ".potx",
    ".potm",
    ".ppsx",
    ".ppsm",
}
IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}


def _temporary_sibling(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def _operation_result(
    operation: str,
    source: Path,
    output: Path,
    execution: dict[str, object],
    finalizer: str,
    producer: str,
) -> dict[str, object]:
    return {
        "operation": operation,
        "source": file_fact(source),
        "output": file_fact(output),
        "provenance": artifact_provenance(
            output,
            finalizer=finalizer,
            producer=producer,
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


def _render_ooxml(source: Path, output: Path, temporary: Path) -> dict[str, object]:
    work_root = Path("/workspace/work")
    descriptor, script_name = tempfile.mkstemp(
        prefix=".nh-render-", suffix=".js", dir=work_root
    )
    script = Path(script_name)
    manifest = temporary.with_suffix(temporary.suffix + ".manifest.json")
    program = """var inputPath = Argument[\"input\"];
var outputPath = Argument[\"output\"];
builder.OpenFile(\"jsValue(inputPath)\");
builder.SaveFile(\"pdf\", \"jsValue(outputPath)\");
builder.CloseFile();
"""
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(program)
            target.flush()
            os.fsync(target.fileno())
        execution = run_utility(
            [
                "nh-office",
                "run",
                str(script),
                "--input",
                str(source),
                "--output",
                str(temporary),
                "--manifest",
                str(manifest),
            ],
            timeout=600,
        )
    finally:
        script.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
    return execution


def render_pdf(source: Path, output: Path) -> dict[str, object]:
    if output.suffix.lower() != ".pdf":
        raise ValueError("rendered document output must use a .pdf extension")
    temporary = _temporary_sibling(output)
    try:
        extension = source.suffix.lower()
        if extension in OOXML_EXTENSIONS:
            execution = _render_ooxml(source, output, temporary)
            finalizer = "office_render"
            producer = "nh-office:docbuilder"
        elif extension == ".pdf":
            execution = run_utility(
                ["qpdf", str(source), "--", str(temporary)], timeout=180
            )
            finalizer = "pdf_page"
            producer = "qpdf"
        elif extension in IMAGE_EXTENSIONS:
            if extension == ".svg":
                execution = run_utility(
                    ["rsvg-convert", "--format=pdf", "--output", str(temporary), str(source)],
                    timeout=180,
                )
                producer = "rsvg-convert"
            else:
                execution = run_utility(
                    ["img2pdf", "--output", str(temporary), "--", str(source)],
                    timeout=180,
                )
                producer = "img2pdf"
            finalizer = "pdf_render"
        else:
            raise ValueError(
                "render supports OOXML documents, presentations, spreadsheets, PDFs, and common images"
            )
        if not temporary.is_file() or temporary.is_symlink():
            raise UtilityError("render did not produce a regular PDF")
        validation = run_utility(["qpdf", "--check", str(temporary)], timeout=60)
        os.replace(temporary, output)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    result = _operation_result(
        "render_pdf",
        source,
        output,
        execution,
        finalizer=finalizer,
        producer=producer,
    )
    result["validation"] = {
        "command": validation["command"],
        "returncode": validation["returncode"],
        "duration_ms": validation["duration_ms"],
    }
    return result


def render_page(
    source: Path,
    output: Path,
    *,
    page: int = 1,
    dpi: int = 144,
) -> dict[str, object]:
    if page < 1:
        raise ValueError("page number must be at least 1")
    if not 36 <= dpi <= 600:
        raise ValueError("render DPI must be between 36 and 600")
    if output.suffix.lower() not in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        raise ValueError("page image output must be PNG, JPEG, or TIFF")

    temporary_pdf: Path | None = None
    pdf = source
    if source.suffix.lower() != ".pdf":
        descriptor, name = tempfile.mkstemp(
            prefix=".nh-page-source-", suffix=".pdf", dir="/workspace/output"
        )
        os.close(descriptor)
        temporary_pdf = Path(name)
        temporary_pdf.unlink()
        render_pdf(source, temporary_pdf)
        pdf = temporary_pdf

    temporary = _temporary_sibling(output)
    prefix = temporary.with_suffix("")
    format_name = "png"
    options: list[str] = ["-png"]
    if output.suffix.lower() in {".jpg", ".jpeg"}:
        format_name = "jpg"
        options = ["-jpeg"]
    elif output.suffix.lower() in {".tif", ".tiff"}:
        format_name = "tif"
        options = ["-tiff"]
    produced = Path(f"{prefix}.{format_name}")
    try:
        execution = run_utility(
            [
                "pdftoppm",
                *options,
                "-r",
                str(dpi),
                "-f",
                str(page),
                "-l",
                str(page),
                "-singlefile",
                str(pdf),
                str(prefix),
            ],
            timeout=300,
        )
        if not produced.is_file() or produced.is_symlink():
            raise UtilityError("page render did not produce a regular image")
        os.replace(produced, output)
    finally:
        for candidate in (temporary, produced):
            if candidate.exists() or candidate.is_symlink():
                candidate.unlink()
        if temporary_pdf is not None:
            temporary_pdf.unlink(missing_ok=True)
    result = _operation_result(
        "render_page",
        source,
        output,
        execution,
        finalizer="preview_only",
        producer="pdftoppm",
    )
    result["page"] = page
    result["dpi"] = dpi
    return result


def before_after(
    before: Path,
    after: Path,
    output: Path,
    *,
    page: int = 1,
    dpi: int = 120,
) -> dict[str, object]:
    if output.suffix.lower() != ".png":
        raise ValueError("before/after output must use a .png extension")
    before_image = _temporary_sibling(output.with_name(output.stem + "-before.png"))
    after_image = _temporary_sibling(output.with_name(output.stem + "-after.png"))
    temporary = _temporary_sibling(output)
    try:
        before_result = render_page(before, before_image, page=page, dpi=dpi)
        after_result = render_page(after, after_image, page=page, dpi=dpi)
        magick = shutil.which("magick") or shutil.which("convert")
        if magick is None:
            raise UtilityError("ImageMagick is not installed")
        execution = run_utility(
            [
                magick,
                str(before_image),
                str(after_image),
                "+append",
                str(temporary),
            ],
            timeout=180,
        )
        if not temporary.is_file() or temporary.is_symlink():
            raise UtilityError("before/after render did not produce an image")
        os.replace(temporary, output)
    finally:
        for path in (before_image, after_image, temporary):
            if path.exists() or path.is_symlink():
                path.unlink()
    result = _operation_result(
        "before_after",
        before,
        output,
        execution,
        finalizer="preview_only",
        producer=Path(str(execution["command"][0])).name,
    )
    result["after"] = file_fact(after)
    result["page"] = page
    result["dpi"] = dpi
    result["components"] = {"before": before_result, "after": after_result}
    return result
