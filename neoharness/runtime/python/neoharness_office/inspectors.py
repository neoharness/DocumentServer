from __future__ import annotations

import hashlib
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, deque
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from .images import image_info
from .paths import file_fact
from .process import run_utility

MAX_XML_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 100_000
MAX_ZIP_UNCOMPRESSED = 8 * 1024 * 1024 * 1024
MAX_TEXT_SAMPLE = 8_000
MAX_CELL_SAMPLES = 240
MAX_LIST_ITEMS = 500
MAX_COMPARISON_CHANGES = 2_000

NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
}
CELL_REF = re.compile(r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*)$")
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
    ".dng",
    ".nef",
    ".cr2",
    ".cr3",
    ".arw",
}


class InspectionError(ValueError):
    """A document cannot be safely inspected as the claimed package type."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _bounded_text(value: str, limit: int = MAX_TEXT_SAMPLE) -> str:
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + "…"


def _read_part(
    archive: zipfile.ZipFile, name: str, *, limit: int = MAX_XML_BYTES
) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise InspectionError(f"OOXML package is missing {name}") from exc
    if info.file_size > limit:
        raise InspectionError(f"OOXML part exceeds inspection limit: {name}")
    data = archive.read(info)
    if b"<!DOCTYPE" in data.upper():
        raise InspectionError(f"OOXML part contains a prohibited document type: {name}")
    return data


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(_read_part(archive, name))
    except ET.ParseError as exc:
        raise InspectionError(
            f"OOXML part is not well-formed XML: {name}: {exc}"
        ) from exc


def _archive_facts(archive: zipfile.ZipFile) -> dict[str, object]:
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise InspectionError("OOXML package contains too many parts")
    total = sum(info.file_size for info in infos)
    if total > MAX_ZIP_UNCOMPRESSED:
        raise InspectionError("OOXML package exceeds the uncompressed inspection limit")
    names = [info.filename for info in infos]
    unsafe = [
        name
        for name in names
        if name.startswith("/") or ".." in PurePosixPath(name).parts
    ]
    macros = [name for name in names if name.lower().endswith("vbaproject.bin")]
    embedded = [
        name
        for name in names
        if "/embeddings/" in f"/{name.lower()}"
        or name.lower().endswith("oleobject.bin")
    ]
    return {
        "entries": len(infos),
        "compressed_bytes": sum(info.compress_size for info in infos),
        "uncompressed_bytes": total,
        "unsafe_part_names": unsafe[:MAX_LIST_ITEMS],
        "macro_parts": macros[:MAX_LIST_ITEMS],
        "macro_bearing": bool(macros),
        "embedded_object_parts": embedded[:MAX_LIST_ITEMS],
    }


def _relationship_facts(archive: zipfile.ZipFile) -> dict[str, object]:
    external: list[dict[str, str]] = []
    relationship_count = 0
    for info in archive.infolist():
        if not info.filename.endswith(".rels"):
            continue
        try:
            root = _xml(archive, info.filename)
        except InspectionError:
            continue
        for relationship in root:
            relationship_count += 1
            if relationship.attrib.get("TargetMode") == "External":
                external.append(
                    {
                        "part": info.filename,
                        "id": relationship.attrib.get("Id", ""),
                        "type": relationship.attrib.get("Type", ""),
                        "target": relationship.attrib.get("Target", ""),
                    }
                )
    return {
        "count": relationship_count,
        "external": external[:MAX_LIST_ITEMS],
        "external_count": len(external),
    }


def _resolve_relationship(source: str, target: str) -> str:
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            str(PurePosixPath(source).parent.joinpath(target))
        )
    if resolved == ".." or resolved.startswith("../"):
        raise InspectionError("OOXML relationship escapes the package root")
    return resolved


def _relationship_map(
    archive: zipfile.ZipFile, source: str, rels_name: str
) -> dict[str, str]:
    if rels_name not in archive.namelist():
        return {}
    result: dict[str, str] = {}
    for relationship in _xml(archive, rels_name):
        identifier = relationship.attrib.get("Id")
        target = relationship.attrib.get("Target")
        if (
            identifier
            and target
            and relationship.attrib.get("TargetMode") != "External"
        ):
            result[identifier] = _resolve_relationship(source, target)
    return result


def _column_index(label: str) -> int:
    value = 0
    for character in label:
        value = value * 26 + ord(character) - 64
    return value


def _cell_coordinate(reference: str) -> tuple[int, int] | None:
    match = CELL_REF.match(reference.upper())
    if not match:
        return None
    return int(match.group(2)), _column_index(match.group(1))


def _intervals(values: Iterable[int]) -> list[tuple[int, int]]:
    ordered = sorted(set(values))
    if not ordered:
        return []
    result: list[tuple[int, int]] = []
    start = previous = ordered[0]
    for value in ordered[1:]:
        if value > previous + 1:
            result.append((start, previous))
            start = value
        previous = value
    result.append((start, previous))
    return result


def _regions(cells: list[tuple[int, int]]) -> list[dict[str, object]]:
    if not cells:
        return []
    if len(cells) > 200_000:
        rows = [row for row, _ in cells]
        columns = [column for _, column in cells]
        return [
            {
                "row_start": min(rows),
                "row_end": max(rows),
                "column_start": min(columns),
                "column_end": max(columns),
                "occupied_cells": len(cells),
                "strategy": "bounded_whole_sheet",
            }
        ]

    occupied = set(cells)
    components: list[dict[str, object]] = []
    while occupied and len(components) < 100:
        start = occupied.pop()
        queue = deque([start])
        rows: list[int] = []
        columns: list[int] = []
        while queue:
            row, column = queue.popleft()
            rows.append(row)
            columns.append(column)
            for neighbor in (
                (row - 1, column),
                (row + 1, column),
                (row, column - 1),
                (row, column + 1),
            ):
                if neighbor in occupied:
                    occupied.remove(neighbor)
                    queue.append(neighbor)
        area = (max(rows) - min(rows) + 1) * (max(columns) - min(columns) + 1)
        components.append(
            {
                "row_start": min(rows),
                "row_end": max(rows),
                "column_start": min(columns),
                "column_end": max(columns),
                "occupied_cells": len(rows),
                "density": round(len(rows) / area, 4),
                "strategy": "orthogonal_connectivity",
            }
        )
    components.sort(key=lambda item: int(item["occupied_cells"]), reverse=True)
    if occupied:
        components.append(
            {
                "remaining_components_truncated": True,
                "remaining_occupied_cells": len(occupied),
            }
        )
    return components


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    name = "xl/sharedStrings.xml"
    if name not in archive.namelist():
        return []
    root = _xml(archive, name)
    values: list[str] = []
    for item in root.findall("s:si", NS):
        text = "".join(
            node.text or "" for node in item.iter() if _local(node.tag) == "t"
        )
        values.append(text)
        if len(values) >= 500_000:
            break
    return values


def _cell_value(cell: ET.Element, shared: list[str]) -> object:
    kind = cell.attrib.get("t")
    value_node = cell.find("s:v", NS)
    if kind == "inlineStr":
        return "".join(
            node.text or "" for node in cell.iter() if _local(node.tag) == "t"
        )
    if value_node is None or value_node.text is None:
        return None
    value = value_node.text
    if kind == "s":
        try:
            return shared[int(value)]
        except (ValueError, IndexError):
            return value
    if kind == "b":
        return value == "1"
    return value


def _inspect_worksheet(
    archive: zipfile.ZipFile,
    path: str,
    *,
    name: str,
    state: str,
    shared: list[str],
) -> dict[str, object]:
    root = _xml(archive, path)
    cells: list[tuple[int, int]] = []
    samples: list[dict[str, object]] = []
    formula_count = 0
    styled_blank_count = 0
    for cell in root.findall(".//s:c", NS):
        reference = cell.attrib.get("r", "")
        coordinate = _cell_coordinate(reference)
        formula = cell.find("s:f", NS)
        value = _cell_value(cell, shared)
        occupied = formula is not None or value not in (None, "")
        if occupied and coordinate:
            cells.append(coordinate)
        elif "s" in cell.attrib:
            styled_blank_count += 1
        if formula is not None:
            formula_count += 1
        if occupied and len(samples) < MAX_CELL_SAMPLES:
            samples.append(
                {
                    "cell": reference,
                    "value": value,
                    "formula": formula.text if formula is not None else None,
                    "style": cell.attrib.get("s"),
                }
            )
    merges = [node.attrib.get("ref", "") for node in root.findall(".//s:mergeCell", NS)]
    dimension = root.find("s:dimension", NS)
    row_indices = [row for row, _ in cells]
    column_indices = [column for _, column in cells]
    return {
        "name": name,
        "state": state,
        "part": path,
        "declared_dimension": dimension.attrib.get("ref")
        if dimension is not None
        else None,
        "occupied_cells": len(cells),
        "styled_blank_cells": styled_blank_count,
        "formula_cells": formula_count,
        "occupied_rows": len(set(row_indices)),
        "occupied_columns": len(set(column_indices)),
        "row_intervals": _intervals(row_indices)[:MAX_LIST_ITEMS],
        "column_intervals": _intervals(column_indices)[:MAX_LIST_ITEMS],
        "regions": _regions(cells),
        "merged_ranges": merges[:MAX_LIST_ITEMS],
        "merged_range_count": len(merges),
        "data_validation_count": len(root.findall(".//s:dataValidation", NS)),
        "conditional_format_count": len(root.findall(".//s:conditionalFormatting", NS)),
        "hyperlink_count": len(root.findall(".//s:hyperlink", NS)),
        "drawing_count": len(root.findall(".//s:drawing", NS)),
        "table_reference_count": len(root.findall(".//s:tablePart", NS)),
        "custom_row_height_count": sum(
            1 for row in root.findall(".//s:row", NS) if "ht" in row.attrib
        ),
        "custom_column_group_count": len(root.findall(".//s:col", NS)),
        "sample_cells": samples,
    }


def _inspect_xlsx(archive: zipfile.ZipFile) -> dict[str, object]:
    workbook = _xml(archive, "xl/workbook.xml")
    relations = _relationship_map(
        archive, "xl/workbook.xml", "xl/_rels/workbook.xml.rels"
    )
    shared = _shared_strings(archive)
    sheets: list[dict[str, object]] = []
    for sheet in workbook.findall(".//s:sheet", NS):
        identifier = sheet.attrib.get(f"{{{NS['r']}}}id", "")
        path = relations.get(identifier)
        if not path or path not in archive.namelist():
            sheets.append(
                {
                    "name": sheet.attrib.get("name", ""),
                    "state": sheet.attrib.get("state", "visible"),
                    "part": path,
                    "inspection_error": "worksheet relationship is missing",
                }
            )
            continue
        sheets.append(
            _inspect_worksheet(
                archive,
                path,
                name=sheet.attrib.get("name", ""),
                state=sheet.attrib.get("state", "visible"),
                shared=shared,
            )
        )
    names = archive.namelist()
    defined_names = [
        {
            "name": node.attrib.get("name", ""),
            "scope_sheet": node.attrib.get("localSheetId"),
            "value": _bounded_text(node.text or "", 1000),
        }
        for node in workbook.findall(".//s:definedName", NS)
    ]
    return {
        "format": "xlsx",
        "sheets": sheets,
        "sheet_count": len(sheets),
        "defined_names": defined_names[:MAX_LIST_ITEMS],
        "defined_name_count": len(defined_names),
        "table_part_count": sum(name.startswith("xl/tables/") for name in names),
        "chart_part_count": sum(name.startswith("xl/charts/") for name in names),
        "drawing_part_count": sum(name.startswith("xl/drawings/") for name in names),
        "pivot_part_count": sum("pivot" in name.lower() for name in names),
        "external_link_part_count": sum(
            name.startswith("xl/externalLinks/") for name in names
        ),
        "calculation_chain_present": "xl/calcChain.xml" in names,
        "shared_string_count": len(shared),
    }


def _inspect_docx(archive: zipfile.ZipFile) -> dict[str, object]:
    root = _xml(archive, "word/document.xml")
    paragraphs = root.findall(".//w:p", NS)
    text = "\n".join(
        "".join(node.text or "" for node in paragraph.iter() if _local(node.tag) == "t")
        for paragraph in paragraphs
    )
    tables: list[dict[str, int]] = []
    for table in root.findall(".//w:tbl", NS)[:MAX_LIST_ITEMS]:
        rows = table.findall("w:tr", NS)
        tables.append(
            {
                "rows": len(rows),
                "maximum_columns": max(
                    (len(row.findall("w:tc", NS)) for row in rows), default=0
                ),
            }
        )
    names = archive.namelist()
    styles = 0
    if "word/styles.xml" in names:
        styles = len(_xml(archive, "word/styles.xml").findall(".//w:style", NS))
    headers = [name for name in names if re.match(r"word/header\d+\.xml$", name)]
    footers = [name for name in names if re.match(r"word/footer\d+\.xml$", name)]
    return {
        "format": "docx",
        "paragraph_count": len(paragraphs),
        "table_count": len(root.findall(".//w:tbl", NS)),
        "tables": tables,
        "section_count": len(root.findall(".//w:sectPr", NS)),
        "style_count": styles,
        "header_parts": headers,
        "footer_parts": footers,
        "field_instruction_count": len(root.findall(".//w:instrText", NS))
        + len(root.findall(".//w:fldSimple", NS)),
        "content_control_count": len(root.findall(".//w:sdt", NS)),
        "drawing_count": len(root.findall(".//w:drawing", NS))
        + len(root.findall(".//w:pict", NS)),
        "tracked_insert_count": len(root.findall(".//w:ins", NS)),
        "tracked_delete_count": len(root.findall(".//w:del", NS)),
        "bookmark_count": len(root.findall(".//w:bookmarkStart", NS)),
        "comment_part_present": "word/comments.xml" in names,
        "footnote_part_present": "word/footnotes.xml" in names,
        "endnote_part_present": "word/endnotes.xml" in names,
        "numbering_part_present": "word/numbering.xml" in names,
        "text_sample": _bounded_text(text),
    }


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]


def _inspect_pptx(archive: zipfile.ZipFile) -> dict[str, object]:
    names = archive.namelist()
    slide_names = sorted(
        (name for name in names if re.match(r"ppt/slides/slide\d+\.xml$", name)),
        key=_natural_key,
    )
    slides: list[dict[str, object]] = []
    for index, name in enumerate(slide_names[:MAX_LIST_ITEMS], start=1):
        root = _xml(archive, name)
        text = " ".join(node.text or "" for node in root.findall(".//a:t", NS))
        slides.append(
            {
                "index": index,
                "part": name,
                "shape_count": len(root.findall(".//p:sp", NS)),
                "picture_count": len(root.findall(".//p:pic", NS)),
                "graphic_frame_count": len(root.findall(".//p:graphicFrame", NS)),
                "table_count": len(root.findall(".//a:tbl", NS)),
                "text_sample": _bounded_text(text, 1200),
            }
        )
    return {
        "format": "pptx",
        "slide_count": len(slide_names),
        "slides": slides,
        "slide_master_count": sum(
            bool(re.match(r"ppt/slideMasters/slideMaster\d+\.xml$", name))
            for name in names
        ),
        "slide_layout_count": sum(
            bool(re.match(r"ppt/slideLayouts/slideLayout\d+\.xml$", name))
            for name in names
        ),
        "notes_slide_count": sum(
            bool(re.match(r"ppt/notesSlides/notesSlide\d+\.xml$", name))
            for name in names
        ),
        "chart_part_count": sum(name.startswith("ppt/charts/") for name in names),
        "media_part_count": sum(name.startswith("ppt/media/") for name in names),
        "comment_part_count": sum("comments/" in name for name in names),
    }


def _inspect_ooxml(path: Path, extension: str) -> dict[str, object]:
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InspectionError(
            f"file is not a valid OOXML ZIP package: {path.name}"
        ) from exc
    with archive:
        package = _archive_facts(archive)
        relationships = _relationship_facts(archive)
        if extension in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
            semantic = _inspect_xlsx(archive)
        elif extension in {".docx", ".docm", ".dotx", ".dotm"}:
            semantic = _inspect_docx(archive)
        else:
            semantic = _inspect_pptx(archive)
    return {
        "kind": "ooxml",
        "package": package,
        "relationships": relationships,
        "semantic": semantic,
    }


def _parse_key_value(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _inspect_pdf(path: Path) -> dict[str, object]:
    info_result = run_utility(["pdfinfo", str(path)], timeout=30, check=False)
    info = _parse_key_value(str(info_result["stdout"]))
    try:
        pages = int(info.get("Pages", "0"))
    except ValueError:
        pages = 0
    last_page = max(1, min(pages or 5, 5))
    text_result = run_utility(
        ["pdftotext", "-f", "1", "-l", str(last_page), str(path), "-"],
        timeout=60,
        check=False,
    )
    extracted = str(text_result["stdout"])
    image_result = run_utility(
        ["pdfimages", "-list", str(path)], timeout=30, check=False
    )
    image_lines = [
        line
        for line in str(image_result["stdout"]).splitlines()
        if re.match(r"^\s*\d+\s+\d+\s+", line)
    ]
    font_result = run_utility(["pdffonts", str(path)], timeout=30, check=False)
    font_lines = [
        line
        for line in str(font_result["stdout"]).splitlines()
        if line.strip() and not line.startswith("name") and not line.startswith("---")
    ]
    check_result = run_utility(["qpdf", "--check", str(path)], timeout=30, check=False)
    visible_characters = len(re.sub(r"\s+", "", extracted))
    return {
        "kind": "pdf",
        "pages": pages,
        "metadata": info,
        "qpdf_valid": check_result["returncode"] == 0,
        "qpdf_diagnostic": _bounded_text(str(check_result["stderr"]), 2000),
        "embedded_image_count": len(image_lines),
        "font_rows": font_lines[:MAX_LIST_ITEMS],
        "font_count": len(font_lines),
        "sampled_pages": last_page,
        "sampled_visible_characters": visible_characters,
        "text_sample": _bounded_text(extracted),
        "likely_scanned": pages > 0 and visible_characters < 40 and bool(image_lines),
    }


def _file_mime(path: Path) -> str:
    result = run_utility(
        ["file", "--brief", "--mime-type", "--", str(path)], timeout=10
    )
    return str(result["stdout"]).strip() or "application/octet-stream"


def inspect_file(path: Path) -> dict[str, object]:
    mime_type = _file_mime(path)
    result: dict[str, object] = {
        "schema": "ai.neoharness.office.document-inspection.v1",
        "file": file_fact(path, mime_type=mime_type),
    }
    extension = path.suffix.lower()
    if extension in OOXML_EXTENSIONS:
        result["inspection"] = _inspect_ooxml(path, extension)
    elif extension == ".pdf" or mime_type == "application/pdf":
        result["inspection"] = _inspect_pdf(path)
    elif extension in IMAGE_EXTENSIONS or mime_type.startswith("image/"):
        result["inspection"] = {"kind": "image", "image": image_info(path)}
    else:
        result["inspection"] = {
            "kind": "generic",
            "extension": extension,
            "note": "Use the native engine or format-specific utility for deeper inspection.",
        }
    return result


def _part_hashes(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        _archive_facts(archive)
        result: dict[str, str] = {}
        for info in archive.infolist():
            digest = hashlib.sha256()
            with archive.open(info) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[info.filename] = digest.hexdigest()
        return result


def _xlsx_comparison_facts(archive: zipfile.ZipFile) -> dict[str, object]:
    """Return complete workbook authority facts used by revision validation.

    Samples are not sufficient at the publication boundary: a workbook can
    contain formulas far outside the inspector's visible-cell sample.  These
    maps intentionally walk every declared worksheet formula while retaining
    only formula text/attributes rather than cell values or customer content.
    """

    workbook = _xml(archive, "xl/workbook.xml")
    relations = _relationship_map(
        archive, "xl/workbook.xml", "xl/_rels/workbook.xml.rels"
    )
    formulas: dict[tuple[str, str], dict[str, object]] = {}
    sheet_states: dict[str, str] = {}
    for sheet in workbook.findall(".//s:sheet", NS):
        name = sheet.attrib.get("name", "")
        sheet_states[name] = sheet.attrib.get("state", "visible")
        identifier = sheet.attrib.get(f"{{{NS['r']}}}id", "")
        path = relations.get(identifier)
        if not path or path not in archive.namelist():
            continue
        root = _xml(archive, path)
        for cell in root.findall(".//s:c", NS):
            formula = cell.find("s:f", NS)
            reference = cell.attrib.get("r")
            if formula is None or not reference:
                continue
            formulas[(name, reference.upper())] = {
                "text": formula.text or "",
                "attributes": dict(sorted(formula.attrib.items())),
            }
    sheet_order = [
        sheet.attrib.get("name", "") for sheet in workbook.findall(".//s:sheet", NS)
    ]
    defined_names: dict[tuple[str, str | None], str] = {}
    for node in workbook.findall(".//s:definedName", NS):
        raw_scope = node.attrib.get("localSheetId")
        scope: str | None = None
        if raw_scope is not None:
            try:
                scope = sheet_order[int(raw_scope)]
            except (ValueError, IndexError):
                scope = f"#localSheetId:{raw_scope}"
        defined_names[(node.attrib.get("name", ""), scope)] = node.text or ""
    return {
        "formulas": formulas,
        "sheet_states": sheet_states,
        "defined_names": defined_names,
    }


def _bounded_changes(values: list[dict[str, object]]) -> dict[str, object]:
    return {
        "count": len(values),
        "changes": values[:MAX_COMPARISON_CHANGES],
        "truncated": len(values) > MAX_COMPARISON_CHANGES,
    }


def _compare_xlsx_facts(
    before: zipfile.ZipFile, after: zipfile.ZipFile
) -> dict[str, object]:
    before_facts = _xlsx_comparison_facts(before)
    after_facts = _xlsx_comparison_facts(after)

    before_formulas = before_facts["formulas"]
    after_formulas = after_facts["formulas"]
    assert isinstance(before_formulas, dict)
    assert isinstance(after_formulas, dict)
    formula_changes = [
        {
            "sheet": sheet,
            "cell": cell,
            "before": before_formulas.get((sheet, cell)),
            "after": after_formulas.get((sheet, cell)),
        }
        for sheet, cell in sorted(set(before_formulas) | set(after_formulas))
        if before_formulas.get((sheet, cell)) != after_formulas.get((sheet, cell))
    ]

    before_states = before_facts["sheet_states"]
    after_states = after_facts["sheet_states"]
    assert isinstance(before_states, dict)
    assert isinstance(after_states, dict)
    sheet_state_changes = [
        {
            "sheet": name,
            "before": before_states.get(name),
            "after": after_states.get(name),
        }
        for name in sorted(set(before_states) | set(after_states))
        if before_states.get(name) != after_states.get(name)
    ]

    before_names = before_facts["defined_names"]
    after_names = after_facts["defined_names"]
    assert isinstance(before_names, dict)
    assert isinstance(after_names, dict)
    defined_name_changes = [
        {
            "name": name,
            "scope_sheet": scope,
            "before": before_names.get((name, scope)),
            "after": after_names.get((name, scope)),
        }
        for name, scope in sorted(
            set(before_names) | set(after_names),
            key=lambda item: (item[0], item[1] or ""),
        )
        if before_names.get((name, scope)) != after_names.get((name, scope))
    ]
    return {
        "formulas": _bounded_changes(formula_changes),
        "sheet_states": _bounded_changes(sheet_state_changes),
        "defined_names": _bounded_changes(defined_name_changes),
    }


def _sensitive_part_category(name: str) -> str | None:
    lowered = name.casefold()
    categories = (
        ("macro", "vbaproject.bin"),
        ("media", "/media/"),
        ("chart", "/charts/"),
        ("drawing", "/drawings/"),
        ("table", "/tables/"),
        ("pivot", "pivot"),
        ("external_link", "/externallinks/"),
        ("printer_settings", "/printersettings/"),
        ("embedded_object", "/embeddings/"),
        ("activex", "/activex/"),
        ("control_property", "/ctrlprops/"),
    )
    for category, marker in categories:
        if marker in lowered:
            return category
    return None


def _sensitive_part_changes(
    before: dict[str, str], after: dict[str, str]
) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    for name in sorted(set(before) | set(after)):
        category = _sensitive_part_category(name)
        if category is None or before.get(name) == after.get(name):
            continue
        change = "changed"
        if name not in before:
            change = "added"
        elif name not in after:
            change = "removed"
        changes.append(
            {
                "part": name,
                "category": category,
                "change": change,
                "before_sha256": before.get(name),
                "after_sha256": after.get(name),
            }
        )
    return changes


def _external_relationships(
    archive: zipfile.ZipFile,
) -> Counter[tuple[str, str, str, str]]:
    relationships: Counter[tuple[str, str, str, str]] = Counter()
    for info in archive.infolist():
        if not info.filename.endswith(".rels"):
            continue
        try:
            root = _xml(archive, info.filename)
        except InspectionError:
            continue
        for relationship in root:
            if relationship.attrib.get("TargetMode") != "External":
                continue
            relationships[
                (
                    info.filename,
                    relationship.attrib.get("Id", ""),
                    relationship.attrib.get("Type", ""),
                    relationship.attrib.get("Target", ""),
                )
            ] += 1
    return relationships


def _compare_external_relationships(
    before: zipfile.ZipFile, after: zipfile.ZipFile
) -> dict[str, object]:
    before_values = _external_relationships(before)
    after_values = _external_relationships(after)

    def expand(values: Counter[tuple[str, str, str, str]]) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for (part, identifier, kind, target), count in sorted(values.items()):
            rows.append(
                {
                    "part": part,
                    "id": identifier,
                    "type": kind,
                    "target": target,
                    "count": count,
                }
            )
        return rows

    removed = expand(before_values - after_values)
    added = expand(after_values - before_values)
    combined = removed + added
    return {
        "count": len(removed) + len(added),
        "removed": removed[:MAX_COMPARISON_CHANGES],
        "added": added[:MAX_COMPARISON_CHANGES],
        "truncated": len(combined) > MAX_COMPARISON_CHANGES,
    }


def compare_files(before: Path, after: Path) -> dict[str, object]:
    before_fact = file_fact(before, mime_type=_file_mime(before))
    after_fact = file_fact(after, mime_type=_file_mime(after))
    result: dict[str, object] = {
        "schema": "ai.neoharness.office.document-comparison.v2",
        "before": before_fact,
        "after": after_fact,
        "byte_identical": before_fact["sha256"] == after_fact["sha256"],
    }
    if (
        before.suffix.lower() in OOXML_EXTENSIONS
        and after.suffix.lower() in OOXML_EXTENSIONS
    ):
        before_parts = _part_hashes(before)
        after_parts = _part_hashes(after)
        before_names = set(before_parts)
        after_names = set(after_parts)
        changed = sorted(
            name
            for name in before_names & after_names
            if before_parts[name] != after_parts[name]
        )
        macros = sorted(
            name
            for name in before_names | after_names
            if name.lower().endswith("vbaproject.bin")
        )
        sensitive_changes = _sensitive_part_changes(before_parts, after_parts)
        result["package"] = {
            "added_parts": sorted(after_names - before_names),
            "removed_parts": sorted(before_names - after_names),
            "changed_parts": changed,
            "unchanged_part_count": len(before_names & after_names) - len(changed),
            "macro_parts": [
                {
                    "part": name,
                    "before": before_parts.get(name),
                    "after": after_parts.get(name),
                    "preserved": before_parts.get(name) == after_parts.get(name),
                }
                for name in macros
            ],
            "sensitive_part_changes": sensitive_changes[:MAX_COMPARISON_CHANGES],
            "sensitive_part_change_count": len(sensitive_changes),
            "sensitive_part_changes_truncated": (
                len(sensitive_changes) > MAX_COMPARISON_CHANGES
            ),
        }
        with (
            zipfile.ZipFile(before) as before_archive,
            zipfile.ZipFile(after) as after_archive,
        ):
            result["external_relationships"] = _compare_external_relationships(
                before_archive, after_archive
            )
            if before.suffix.lower() in {
                ".xlsx",
                ".xlsm",
                ".xltx",
                ".xltm",
            } and after.suffix.lower() in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
                result["spreadsheet"] = _compare_xlsx_facts(
                    before_archive, after_archive
                )
    return result
