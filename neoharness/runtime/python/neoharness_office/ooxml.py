from __future__ import annotations

import copy
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import tempfile
from typing import Iterable
import xml.etree.ElementTree as ET
import zipfile

from .inspectors import compare_files
from .paths import file_fact
from .quality import artifact_provenance


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
XML = "http://www.w3.org/XML/1998/namespace"

NS = {"w": W, "s": S, "a": A, "p": P, "r": R, "rel": REL}
CELL_REFERENCE = re.compile(r"^([A-Z]{1,3})([1-9][0-9]*)$")
MAX_PACKAGE_ENTRIES = 100_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024

for prefix, uri in (
    ("w", W),
    ("a", A),
    ("p", P),
    ("r", R),
    ("xdr", "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"),
    ("c", "http://schemas.openxmlformats.org/drawingml/2006/chart"),
):
    ET.register_namespace(prefix, uri)


class OoxmlMutationError(ValueError):
    """A requested surgical package operation is ambiguous or unsupported."""


def _xml(data: bytes, part: str) -> ET.Element:
    if b"<!DOCTYPE" in data.upper():
        raise OoxmlMutationError(f"OOXML part contains a prohibited document type: {part}")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise OoxmlMutationError(f"OOXML part is not well formed: {part}: {exc}") from exc


def _serialized(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _validated_archive(path: Path) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise OoxmlMutationError(f"not a valid OOXML package: {path.name}") from exc
    infos = archive.infolist()
    if len(infos) > MAX_PACKAGE_ENTRIES:
        archive.close()
        raise OoxmlMutationError("OOXML package contains too many parts")
    if sum(item.file_size for item in infos) > MAX_UNCOMPRESSED_BYTES:
        archive.close()
        raise OoxmlMutationError("OOXML package exceeds the uncompressed size limit")
    unsafe = [
        item.filename
        for item in infos
        if item.filename.startswith("/")
        or ".." in PurePosixPath(item.filename).parts
    ]
    if unsafe:
        archive.close()
        raise OoxmlMutationError("OOXML package contains an unsafe part name")
    return archive


def _temporary_sibling(output: Path) -> Path:
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=output.suffix, dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def _rewrite_package(
    source: Path, output: Path, replacements: dict[str, bytes]
) -> None:
    temporary = _temporary_sibling(output)
    try:
        with _validated_archive(source) as archive:
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as target:
                target.comment = archive.comment
                for info in archive.infolist():
                    data = replacements.get(info.filename)
                    if data is None:
                        data = archive.read(info)
                    target.writestr(info, data)
        with zipfile.ZipFile(temporary, "r") as verification:
            failure = verification.testzip()
            if failure is not None:
                raise OoxmlMutationError(
                    f"rewritten package failed ZIP verification at {failure}"
                )
        os.replace(temporary, output)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def _set_space_attribute(node: ET.Element) -> None:
    value = node.text or ""
    attribute = f"{{{XML}}}space"
    if value.startswith(" ") or value.endswith(" "):
        node.set(attribute, "preserve")
    else:
        node.attrib.pop(attribute, None)


def _matches(value: str, search: str, *, match_case: bool) -> list[tuple[int, int]]:
    if not search:
        raise OoxmlMutationError("search text cannot be empty")
    flags = 0 if match_case else re.IGNORECASE
    return [
        (match.start(), match.end())
        for match in re.finditer(re.escape(search), value, flags)
    ]


def _replace_text_in_containers(
    root: ET.Element,
    *,
    container_tag: str,
    text_tag: str,
    search: str,
    replacement: str,
    match_case: bool,
    remaining: int | None,
) -> int:
    changed = 0
    for container in root.iter(container_tag):
        nodes = list(container.iter(text_tag))
        if not nodes:
            continue
        values = [node.text or "" for node in nodes]
        combined = "".join(values)
        occurrences = _matches(combined, search, match_case=match_case)
        if remaining is not None:
            occurrences = occurrences[: max(0, remaining - changed)]
        for start, end in reversed(occurrences):
            offsets: list[tuple[int, int]] = []
            cursor = 0
            for value in values:
                offsets.append((cursor, cursor + len(value)))
                cursor += len(value)
            first = next(
                index for index, (_, node_end) in enumerate(offsets) if node_end > start
            )
            last = next(
                index
                for index, (node_start, _) in reversed(list(enumerate(offsets)))
                if node_start < end
            )
            first_start, _ = offsets[first]
            last_start, _ = offsets[last]
            prefix = values[first][: start - first_start]
            suffix = values[last][end - last_start :]
            if first == last:
                values[first] = prefix + replacement + suffix
            else:
                values[first] = prefix + replacement
                for index in range(first + 1, last):
                    values[index] = ""
                values[last] = suffix
            changed += 1
        if occurrences:
            for node, value in zip(nodes, values, strict=True):
                node.text = value
                _set_space_attribute(node)
        if remaining is not None and changed >= remaining:
            break
    return changed


def _docx_text_parts(names: Iterable[str]) -> list[str]:
    result = ["word/document.xml"]
    patterns = (
        r"word/header\d+\.xml$",
        r"word/footer\d+\.xml$",
        r"word/footnotes\.xml$",
        r"word/endnotes\.xml$",
        r"word/comments\.xml$",
    )
    result.extend(
        name
        for name in names
        if name not in result and any(re.match(pattern, name) for pattern in patterns)
    )
    return result


def _mutation_result(
    operation: str,
    source: Path,
    output: Path,
    *,
    details: dict[str, object],
) -> dict[str, object]:
    return {
        "schema": "ai.neoharness.office.surgical-mutation.v1",
        "operation": operation,
        "source": file_fact(source),
        "output": file_fact(output),
        "provenance": artifact_provenance(
            output,
            finalizer="surgical_ooxml",
            producer="nh-document:ooxml",
            operation=operation,
        ),
        "details": details,
        "comparison": compare_files(source, output),
    }


def replace_text(
    source: Path,
    output: Path,
    *,
    search: str,
    replacement: str,
    match_case: bool = True,
    expected_count: int | None = None,
    maximum_count: int | None = None,
) -> dict[str, object]:
    extension = source.suffix.lower()
    replacements: dict[str, bytes] = {}
    changed_parts: list[str] = []
    total = 0
    with _validated_archive(source) as archive:
        names = archive.namelist()
        if extension in {".docx", ".docm", ".dotx", ".dotm"}:
            parts = _docx_text_parts(names)
            container_tag = f"{{{W}}}p"
            text_tag = f"{{{W}}}t"
        elif extension in {".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"}:
            parts = sorted(
                name
                for name in names
                if re.match(
                    r"ppt/(slides|notesSlides|slideMasters|slideLayouts)/[^/]+\.xml$",
                    name,
                )
            )
            container_tag = f"{{{A}}}p"
            text_tag = f"{{{A}}}t"
        else:
            raise OoxmlMutationError(
                "surgical text replacement supports DOCX/DOCM and PPTX/PPTM packages"
            )
        for part in parts:
            if part not in names:
                continue
            root = _xml(archive.read(part), part)
            count = _replace_text_in_containers(
                root,
                container_tag=container_tag,
                text_tag=text_tag,
                search=search,
                replacement=replacement,
                match_case=match_case,
                remaining=(
                    None if maximum_count is None else max(0, maximum_count - total)
                ),
            )
            if count:
                total += count
                changed_parts.append(part)
                replacements[part] = _serialized(root)
            if maximum_count is not None and total >= maximum_count:
                break
    if expected_count is not None and total != expected_count:
        raise OoxmlMutationError(
            f"expected {expected_count} text matches, found {total}; no output was written"
        )
    if total == 0:
        raise OoxmlMutationError("search text was not found; no output was written")
    _rewrite_package(source, output, replacements)
    return _mutation_result(
        "replace_text",
        source,
        output,
        details={"matches": total, "changed_parts": changed_parts},
    )


def update_content_control(
    source: Path,
    output: Path,
    *,
    tag: str,
    value: str,
    expected_count: int | None = None,
) -> dict[str, object]:
    if source.suffix.lower() not in {".docx", ".docm", ".dotx", ".dotm"}:
        raise OoxmlMutationError("content controls require a DOCX/DOCM package")
    replacements: dict[str, bytes] = {}
    changed_parts: list[str] = []
    total = 0
    with _validated_archive(source) as archive:
        for part in _docx_text_parts(archive.namelist()):
            if part not in archive.namelist():
                continue
            root = _xml(archive.read(part), part)
            part_changed = False
            for control in root.iter(f"{{{W}}}sdt"):
                properties = control.find(f"{{{W}}}sdtPr")
                if properties is None:
                    continue
                marker = properties.find(f"{{{W}}}tag")
                if marker is None or marker.attrib.get(f"{{{W}}}val") != tag:
                    continue
                content = control.find(f"{{{W}}}sdtContent")
                if content is None:
                    raise OoxmlMutationError(
                        f"content control {tag!r} has no content container"
                    )
                nodes = list(content.iter(f"{{{W}}}t"))
                if not nodes:
                    paragraph = ET.SubElement(content, f"{{{W}}}p")
                    run = ET.SubElement(paragraph, f"{{{W}}}r")
                    nodes = [ET.SubElement(run, f"{{{W}}}t")]
                nodes[0].text = value
                _set_space_attribute(nodes[0])
                for node in nodes[1:]:
                    node.text = ""
                    _set_space_attribute(node)
                total += 1
                part_changed = True
            if part_changed:
                changed_parts.append(part)
                replacements[part] = _serialized(root)
    if expected_count is not None and total != expected_count:
        raise OoxmlMutationError(
            f"expected {expected_count} content controls with tag {tag!r}, found {total}; no output was written"
        )
    if total == 0:
        raise OoxmlMutationError(
            f"content control tag {tag!r} was not found; no output was written"
        )
    _rewrite_package(source, output, replacements)
    return _mutation_result(
        "update_content_control",
        source,
        output,
        details={"matches": total, "tag": tag, "changed_parts": changed_parts},
    )


def _copy_run_properties(run: ET.Element) -> ET.Element | None:
    properties = run.find(f"{{{W}}}rPr")
    return copy.deepcopy(properties) if properties is not None else None


def _new_run(child: ET.Element, properties: ET.Element | None = None) -> ET.Element:
    run = ET.Element(f"{{{W}}}r")
    if properties is not None:
        run.append(copy.deepcopy(properties))
    run.append(child)
    return run


def replace_text_with_field(
    source: Path,
    output: Path,
    *,
    search: str,
    instruction: str,
    display: str | None = None,
    match_case: bool = True,
    expected_count: int | None = 1,
) -> dict[str, object]:
    if source.suffix.lower() not in {".docx", ".docm", ".dotx", ".dotm"}:
        raise OoxmlMutationError("Word fields require a DOCX/DOCM package")
    if not instruction.strip():
        raise OoxmlMutationError("field instruction cannot be empty")
    replacements: dict[str, bytes] = {}
    changed_parts: list[str] = []
    total = 0
    with _validated_archive(source) as archive:
        names = archive.namelist()
        for part in _docx_text_parts(names):
            if part not in names:
                continue
            root = _xml(archive.read(part), part)
            part_changed = False
            for paragraph in root.iter(f"{{{W}}}p"):
                nodes = list(paragraph.iter(f"{{{W}}}t"))
                if not nodes:
                    continue
                combined = "".join(node.text or "" for node in nodes)
                occurrences = _matches(combined, search, match_case=match_case)
                if not occurrences:
                    continue
                if len(occurrences) > 1:
                    raise OoxmlMutationError(
                        "a paragraph contains more than one matching field target; make the target more specific"
                    )
                start, end = occurrences[0]
                values = [node.text or "" for node in nodes]
                offsets: list[tuple[int, int]] = []
                cursor = 0
                for value in values:
                    offsets.append((cursor, cursor + len(value)))
                    cursor += len(value)
                first = next(i for i, (_, finish) in enumerate(offsets) if finish > start)
                last = next(
                    i
                    for i, (begin, _) in reversed(list(enumerate(offsets)))
                    if begin < end
                )
                first_start, _ = offsets[first]
                last_start, _ = offsets[last]
                prefix = values[first][: start - first_start]
                suffix = values[last][end - last_start :]
                parent_map = {child: parent for parent in root.iter() for child in parent}
                first_run = parent_map.get(nodes[first])
                if first_run is None or first_run.tag != f"{{{W}}}r":
                    raise OoxmlMutationError("field target is not inside a normal Word run")
                run_parent = parent_map.get(first_run)
                if run_parent is None:
                    raise OoxmlMutationError("field target run has no package parent")
                properties = _copy_run_properties(first_run)
                values[first] = prefix
                for index in range(first + 1, last):
                    values[index] = ""
                if first == last:
                    values[first] = prefix
                else:
                    values[last] = suffix
                for node, value in zip(nodes, values, strict=True):
                    node.text = value
                    _set_space_attribute(node)

                runs = [
                    _new_run(
                        ET.Element(f"{{{W}}}fldChar", {f"{{{W}}}fldCharType": "begin"})
                    ),
                    _new_run(
                        ET.Element(
                            f"{{{W}}}instrText",
                            {f"{{{XML}}}space": "preserve"},
                        )
                    ),
                    _new_run(
                        ET.Element(
                            f"{{{W}}}fldChar", {f"{{{W}}}fldCharType": "separate"}
                        )
                    ),
                    _new_run(ET.Element(f"{{{W}}}t"), properties),
                    _new_run(
                        ET.Element(f"{{{W}}}fldChar", {f"{{{W}}}fldCharType": "end"})
                    ),
                ]
                runs[1].find(f"{{{W}}}instrText").text = f" {instruction.strip()} "
                runs[3].find(f"{{{W}}}t").text = display if display is not None else search
                insertion = list(run_parent).index(first_run) + 1
                for run in runs:
                    run_parent.insert(insertion, run)
                    insertion += 1
                if first == last and suffix:
                    trailing = ET.Element(f"{{{W}}}t")
                    trailing.text = suffix
                    _set_space_attribute(trailing)
                    run_parent.insert(insertion, _new_run(trailing, properties))
                total += 1
                part_changed = True
            if part_changed:
                replacements[part] = _serialized(root)
                changed_parts.append(part)

        settings_name = "word/settings.xml"
        if total and settings_name in names:
            settings = _xml(archive.read(settings_name), settings_name)
            update = settings.find(f"{{{W}}}updateFields")
            if update is None:
                update = ET.SubElement(settings, f"{{{W}}}updateFields")
            update.set(f"{{{W}}}val", "true")
            replacements[settings_name] = _serialized(settings)
            changed_parts.append(settings_name)

    if expected_count is not None and total != expected_count:
        raise OoxmlMutationError(
            f"expected {expected_count} field targets, found {total}; no output was written"
        )
    if total == 0:
        raise OoxmlMutationError("field target was not found; no output was written")
    _rewrite_package(source, output, replacements)
    return _mutation_result(
        "replace_text_with_field",
        source,
        output,
        details={
            "matches": total,
            "instruction": instruction.strip(),
            "changed_parts": sorted(set(changed_parts)),
        },
    )


def _relationship_target(source: str, target: str) -> str:
    if target.startswith("/"):
        resolved = posixpath.normpath(target.lstrip("/"))
    else:
        resolved = posixpath.normpath(
            str(PurePosixPath(source).parent.joinpath(target))
        )
    if resolved == ".." or resolved.startswith("../"):
        raise OoxmlMutationError("worksheet relationship escapes the package root")
    return resolved


def _column_number(label: str) -> int:
    value = 0
    for character in label:
        value = value * 26 + ord(character) - 64
    return value


def _cell_key(reference: str) -> tuple[int, int]:
    match = CELL_REFERENCE.match(reference.upper())
    if match is None:
        raise OoxmlMutationError(f"invalid A1 cell reference: {reference}")
    return int(match.group(2)), _column_number(match.group(1))


def _find_or_create_row(sheet_data: ET.Element, row_number: int) -> ET.Element:
    for index, row in enumerate(list(sheet_data)):
        existing = int(row.attrib.get("r", "0"))
        if existing == row_number:
            return row
        if existing > row_number:
            row = ET.Element(f"{{{S}}}row", {"r": str(row_number)})
            sheet_data.insert(index, row)
            return row
    return ET.SubElement(sheet_data, f"{{{S}}}row", {"r": str(row_number)})


def _find_or_create_cell(row: ET.Element, reference: str) -> ET.Element:
    target_key = _cell_key(reference)
    for index, cell in enumerate(list(row)):
        existing_reference = cell.attrib.get("r")
        if not existing_reference:
            continue
        existing_key = _cell_key(existing_reference)
        if existing_key == target_key:
            return cell
        if existing_key > target_key:
            cell = ET.Element(f"{{{S}}}c", {"r": reference.upper()})
            row.insert(index, cell)
            return cell
    return ET.SubElement(row, f"{{{S}}}c", {"r": reference.upper()})


def _set_cell(cell: ET.Element, update: object, style: str | None) -> None:
    for child in list(cell):
        if child.tag in {f"{{{S}}}f", f"{{{S}}}v", f"{{{S}}}is"}:
            cell.remove(child)
    cell.attrib.pop("t", None)
    if style is not None:
        cell.set("s", style)
    if update is None or (isinstance(update, dict) and update.get("clear") is True):
        return
    formula: object | None = None
    value: object = update
    cached: object | None = None
    if isinstance(update, dict):
        unknown = set(update) - {"value", "formula", "cached", "clear", "style_from"}
        if unknown:
            raise OoxmlMutationError(
                "unsupported cell update keys: " + ", ".join(sorted(unknown))
            )
        formula = update.get("formula")
        value = update.get("value")
        cached = update.get("cached")
    if formula is not None:
        text = str(formula)
        ET.SubElement(cell, f"{{{S}}}f").text = text[1:] if text.startswith("=") else text
        if cached is not None:
            ET.SubElement(cell, f"{{{S}}}v").text = str(cached)
        return
    if isinstance(value, bool):
        cell.set("t", "b")
        ET.SubElement(cell, f"{{{S}}}v").text = "1" if value else "0"
    elif isinstance(value, (int, float)):
        ET.SubElement(cell, f"{{{S}}}v").text = str(value)
    elif value is not None:
        cell.set("t", "inlineStr")
        inline = ET.SubElement(cell, f"{{{S}}}is")
        text = ET.SubElement(inline, f"{{{S}}}t")
        text.text = str(value)
        _set_space_attribute(text)


def update_xlsx_cells(
    source: Path,
    output: Path,
    *,
    updates: dict[str, dict[str, object]],
) -> dict[str, object]:
    if source.suffix.lower() not in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        raise OoxmlMutationError("cell updates require an XLSX/XLSM package")
    if not updates:
        raise OoxmlMutationError("cell update plan cannot be empty")
    replacements: dict[str, bytes] = {}
    changed_cells: list[dict[str, str]] = []
    formula_changed = False
    with _validated_archive(source) as archive:
        names = archive.namelist()
        workbook = _xml(archive.read("xl/workbook.xml"), "xl/workbook.xml")
        relationships = _xml(
            archive.read("xl/_rels/workbook.xml.rels"),
            "xl/_rels/workbook.xml.rels",
        )
        targets = {
            node.attrib.get("Id", ""): _relationship_target(
                "xl/workbook.xml", node.attrib.get("Target", "")
            )
            for node in relationships
            if node.attrib.get("TargetMode") != "External"
        }
        sheets = {
            node.attrib.get("name", ""): targets.get(node.attrib.get(f"{{{R}}}id", ""))
            for node in workbook.findall(".//s:sheet", NS)
        }
        for sheet_name, cell_updates in updates.items():
            part = sheets.get(sheet_name)
            if not part or part not in names:
                raise OoxmlMutationError(f"worksheet does not exist: {sheet_name}")
            root = _xml(archive.read(part), part)
            sheet_data = root.find(f"{{{S}}}sheetData")
            if sheet_data is None:
                sheet_data = ET.SubElement(root, f"{{{S}}}sheetData")
            existing_cells = {
                cell.attrib.get("r", "").upper(): cell
                for cell in root.findall(".//s:c", NS)
                if cell.attrib.get("r")
            }
            for reference, update in sorted(cell_updates.items(), key=lambda item: _cell_key(item[0])):
                normalized = reference.upper()
                row_number, _ = _cell_key(normalized)
                style: str | None = None
                if isinstance(update, dict) and update.get("style_from") is not None:
                    style_source = str(update["style_from"]).upper()
                    source_cell = existing_cells.get(style_source)
                    if source_cell is None:
                        raise OoxmlMutationError(
                            f"style source {style_source} does not exist on {sheet_name}"
                        )
                    style = source_cell.attrib.get("s")
                row = _find_or_create_row(sheet_data, row_number)
                cell = _find_or_create_cell(row, normalized)
                if style is None:
                    style = cell.attrib.get("s")
                _set_cell(cell, update, style)
                existing_cells[normalized] = cell
                if isinstance(update, dict) and update.get("formula") is not None:
                    formula_changed = True
                changed_cells.append({"sheet": sheet_name, "cell": normalized})
            replacements[part] = _serialized(root)
        if formula_changed:
            calculation = workbook.find(f"{{{S}}}calcPr")
            if calculation is None:
                calculation = ET.SubElement(workbook, f"{{{S}}}calcPr")
            calculation.set("fullCalcOnLoad", "1")
            calculation.set("forceFullCalc", "1")
            replacements["xl/workbook.xml"] = _serialized(workbook)
    _rewrite_package(source, output, replacements)
    return _mutation_result(
        "update_xlsx_cells",
        source,
        output,
        details={
            "changed_cell_count": len(changed_cells),
            "changed_cells": changed_cells,
            "formula_recalculation_requested": formula_changed,
        },
    )


def load_update_plan(path: Path) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OoxmlMutationError(f"cell plan must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(
        isinstance(sheet, str) and isinstance(cells, dict)
        for sheet, cells in value.items()
    ):
        raise OoxmlMutationError(
            "cell plan must map worksheet names to A1-reference update objects"
        )
    return value
