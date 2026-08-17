from __future__ import annotations

import copy
import io
import json
import math
import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

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
CELL_RANGE = re.compile(
    r"^\$?([A-Z]{1,3})\$?([1-9][0-9]*)(?::\$?([A-Z]{1,3})\$?([1-9][0-9]*))?$"
)
COLOR_HEX = re.compile(r"^(?:[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")
MAX_PACKAGE_ENTRIES = 100_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024

_NAMESPACE_MAPS: dict[int, tuple[tuple[str, str], ...]] = {}

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
        raise OoxmlMutationError(
            f"OOXML part contains a prohibited document type: {part}"
        )
    try:
        namespaces: list[tuple[str, str]] = []
        for _, declaration in ET.iterparse(io.BytesIO(data), events=("start-ns",)):
            if declaration not in namespaces:
                namespaces.append(declaration)
        root = ET.fromstring(data)
        _NAMESPACE_MAPS[id(root)] = tuple(namespaces)
        return root
    except ET.ParseError as exc:
        raise OoxmlMutationError(
            f"OOXML part is not well formed: {part}: {exc}"
        ) from exc


def _serialized(root: ET.Element) -> bytes:
    namespaces = _NAMESPACE_MAPS.get(id(root), ())
    for prefix, uri in namespaces:
        if prefix == "xml":
            continue
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            # ElementTree reserves generated prefixes such as ns0.  They do
            # not need registration, but the declaration is still restored
            # below when it was present on the source root.
            pass
    encoded = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    root_start = encoded.find(b"<", encoded.find(b"?>") + 2)
    root_end = encoded.find(b">", root_start)
    if root_start < 0 or root_end < 0:
        raise OoxmlMutationError("serialized OOXML part has no document element")
    declarations: list[bytes] = []
    opening = encoded[root_start:root_end]
    for prefix, uri in namespaces:
        name = b"xmlns" if not prefix else f"xmlns:{prefix}".encode("utf-8")
        if name + b"=" in opening:
            continue
        escaped = (
            uri.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        declarations.append(b" " + name + b'="' + escaped.encode("utf-8") + b'"')
    if declarations:
        encoded = encoded[:root_end] + b"".join(declarations) + encoded[root_end:]
    return encoded


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
        if item.filename.startswith("/") or ".." in PurePosixPath(item.filename).parts
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
                first = next(
                    i for i, (_, finish) in enumerate(offsets) if finish > start
                )
                last = next(
                    i
                    for i, (begin, _) in reversed(list(enumerate(offsets)))
                    if begin < end
                )
                first_start, _ = offsets[first]
                last_start, _ = offsets[last]
                prefix = values[first][: start - first_start]
                suffix = values[last][end - last_start :]
                parent_map = {
                    child: parent for parent in root.iter() for child in parent
                }
                first_run = parent_map.get(nodes[first])
                if first_run is None or first_run.tag != f"{{{W}}}r":
                    raise OoxmlMutationError(
                        "field target is not inside a normal Word run"
                    )
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
                runs[3].find(f"{{{W}}}t").text = (
                    display if display is not None else search
                )
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
    if isinstance(update, str) and update.startswith("="):
        raise OoxmlMutationError(
            "a scalar value beginning with '=' is ambiguous; use "
            '{"formula": "=A1+B1"} for a formula or '
            '{"value": "=literal text"} for literal text'
        )
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
        ET.SubElement(cell, f"{{{S}}}f").text = text.removeprefix("=")
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
            for reference, update in sorted(
                cell_updates.items(), key=lambda item: _cell_key(item[0])
            ):
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
                changed_cells.append({"sheet": sheet_name, "cell": normalized})
            replacements[part] = _serialized(root)
        # Any changed input can invalidate cached results in dependent formula
        # cells even when no formula string itself changed.  Always request a
        # full calculation on open; stale cached values are not a safe default.
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
            "formula_recalculation_requested": True,
        },
    )


def _style_section(root: ET.Element, name: str) -> ET.Element:
    tag = f"{{{S}}}{name}"
    existing = root.find(tag)
    if existing is not None:
        return existing
    order = [
        "numFmts",
        "fonts",
        "fills",
        "borders",
        "cellStyleXfs",
        "cellXfs",
        "cellStyles",
        "dxfs",
        "tableStyles",
        "colors",
        "extLst",
    ]
    section = ET.Element(tag, {"count": "0"})
    target_index = order.index(name)
    for index, child in enumerate(list(root)):
        local = child.tag.rsplit("}", 1)[-1]
        if local in order and order.index(local) > target_index:
            root.insert(index, section)
            return section
    root.append(section)
    return section


def _component_index(section: ET.Element, component: ET.Element) -> int:
    encoded = _serialized(component)
    for index, existing in enumerate(list(section)):
        if _serialized(existing) == encoded:
            return index
    section.append(component)
    section.set("count", str(len(section)))
    return len(section) - 1


def _style_color(value: object, label: str) -> str:
    if not isinstance(value, str) or COLOR_HEX.fullmatch(value) is None:
        raise OoxmlMutationError(f"{label} must be a 6- or 8-digit hexadecimal color")
    normalized = value.upper()
    return normalized if len(normalized) == 8 else "FF" + normalized


def _set_font_toggle(font: ET.Element, name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise OoxmlMutationError(f"font.{name} must be true or false")
    tag = f"{{{S}}}{name}"
    existing = font.find(tag)
    if value:
        if existing is None:
            font.append(ET.Element(tag, {"val": "1"}))
        else:
            existing.set("val", "1")
    elif existing is not None:
        font.remove(existing)


def _set_font_value(
    font: ET.Element,
    name: str,
    value: object,
    *,
    maximum: int = 255,
) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise OoxmlMutationError(f"font.{name} must be a bounded non-empty string")
    tag = f"{{{S}}}{name}"
    existing = font.find(tag)
    if existing is None:
        existing = ET.SubElement(font, tag)
    existing.set("val", value)


def _font_component(base: ET.Element, plan: object) -> ET.Element:
    if not isinstance(plan, dict):
        raise OoxmlMutationError("font must be an object")
    allowed = {"name", "size", "bold", "italic", "underline", "strike", "color"}
    unknown = set(plan) - allowed
    if unknown:
        raise OoxmlMutationError(
            "unsupported font fields: " + ", ".join(sorted(unknown))
        )
    font = copy.deepcopy(base)
    if "name" in plan:
        _set_font_value(font, "name", plan["name"])
    if "size" in plan:
        size = plan["size"]
        if (
            isinstance(size, bool)
            or not isinstance(size, (int, float))
            or not math.isfinite(float(size))
            or not 1 <= float(size) <= 409
        ):
            raise OoxmlMutationError("font.size must be a number from 1 through 409")
        tag = f"{{{S}}}sz"
        node = font.find(tag)
        if node is None:
            node = ET.SubElement(font, tag)
        node.set("val", str(size))
    for key, element in (
        ("bold", "b"),
        ("italic", "i"),
        ("underline", "u"),
        ("strike", "strike"),
    ):
        if key in plan:
            _set_font_toggle(font, element, plan[key])
    if "color" in plan:
        for old in list(font.findall(f"{{{S}}}color")):
            font.remove(old)
        font.append(
            ET.Element(
                f"{{{S}}}color",
                {"rgb": _style_color(plan["color"], "font.color")},
            )
        )
    return font


def _fill_component(plan: object) -> ET.Element:
    if isinstance(plan, str):
        color = plan
        pattern = "solid"
    elif isinstance(plan, dict) and set(plan) <= {"color", "pattern"}:
        color = plan.get("color")
        pattern = plan.get("pattern", "solid")
    else:
        raise OoxmlMutationError("fill must be a color string or {color,pattern}")
    if pattern not in {
        "solid",
        "darkGray",
        "mediumGray",
        "lightGray",
        "gray125",
        "gray0625",
    }:
        raise OoxmlMutationError("fill.pattern is not supported")
    fill = ET.Element(f"{{{S}}}fill")
    pattern_fill = ET.SubElement(
        fill, f"{{{S}}}patternFill", {"patternType": str(pattern)}
    )
    if color is not None:
        ET.SubElement(
            pattern_fill,
            f"{{{S}}}fgColor",
            {"rgb": _style_color(color, "fill.color")},
        )
        ET.SubElement(pattern_fill, f"{{{S}}}bgColor", {"indexed": "64"})
    return fill


def _border_component(base: ET.Element, plan: object) -> ET.Element:
    if not isinstance(plan, dict):
        raise OoxmlMutationError("border must be an object")
    sides = {"left", "right", "top", "bottom", "diagonal"}
    if set(plan) <= {"style", "color"}:
        specs = {side: plan for side in ("left", "right", "top", "bottom")}
    elif set(plan) <= sides:
        specs = plan
    else:
        raise OoxmlMutationError(
            "border uses style/color or per-side left/right/top/bottom objects"
        )
    valid_styles = {
        "none",
        "hair",
        "dotted",
        "dashDotDot",
        "dashDot",
        "dashed",
        "thin",
        "mediumDashDotDot",
        "slantDashDot",
        "mediumDashDot",
        "mediumDashed",
        "medium",
        "thick",
        "double",
    }
    border = copy.deepcopy(base)
    for side, raw in specs.items():
        if not isinstance(raw, dict) or set(raw) - {"style", "color"}:
            raise OoxmlMutationError(f"border.{side} must contain style/color")
        style = raw.get("style", "thin")
        if style not in valid_styles:
            raise OoxmlMutationError(f"border.{side}.style is not supported")
        tag = f"{{{S}}}{side}"
        node = border.find(tag)
        if node is None:
            node = ET.SubElement(border, tag)
        node.attrib.pop("style", None)
        if style != "none":
            node.set("style", str(style))
        for old in list(node.findall(f"{{{S}}}color")):
            node.remove(old)
        if "color" in raw and style != "none":
            ET.SubElement(
                node,
                f"{{{S}}}color",
                {"rgb": _style_color(raw["color"], f"border.{side}.color")},
            )
    return border


def _alignment_component(base_xf: ET.Element, plan: object) -> ET.Element:
    if not isinstance(plan, dict):
        raise OoxmlMutationError("alignment must be an object")
    allowed = {
        "horizontal",
        "vertical",
        "wrap_text",
        "shrink_to_fit",
        "text_rotation",
        "indent",
    }
    unknown = set(plan) - allowed
    if unknown:
        raise OoxmlMutationError(
            "unsupported alignment fields: " + ", ".join(sorted(unknown))
        )
    existing = base_xf.find(f"{{{S}}}alignment")
    alignment = (
        copy.deepcopy(existing)
        if existing is not None
        else ET.Element(f"{{{S}}}alignment")
    )
    if "horizontal" in plan:
        value = plan["horizontal"]
        if value not in {
            "general",
            "left",
            "center",
            "right",
            "fill",
            "justify",
            "centerContinuous",
            "distributed",
        }:
            raise OoxmlMutationError("alignment.horizontal is not supported")
        alignment.set("horizontal", str(value))
    if "vertical" in plan:
        value = plan["vertical"]
        if value not in {"top", "center", "bottom", "justify", "distributed"}:
            raise OoxmlMutationError("alignment.vertical is not supported")
        alignment.set("vertical", str(value))
    for key, attribute in (
        ("wrap_text", "wrapText"),
        ("shrink_to_fit", "shrinkToFit"),
    ):
        if key in plan:
            if not isinstance(plan[key], bool):
                raise OoxmlMutationError(f"alignment.{key} must be true or false")
            alignment.set(attribute, "1" if plan[key] else "0")
    for key, attribute, minimum, maximum in (
        ("text_rotation", "textRotation", 0, 180),
        ("indent", "indent", 0, 250),
    ):
        if key in plan:
            value = plan[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise OoxmlMutationError(
                    f"alignment.{key} must be an integer from {minimum} through {maximum}"
                )
            alignment.set(attribute, str(value))
    return alignment


def _number_format_id(styles: ET.Element, value: object) -> int:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise OoxmlMutationError("number_format must be a bounded non-empty string")
    formats = _style_section(styles, "numFmts")
    for node in formats.findall(f"{{{S}}}numFmt"):
        if node.attrib.get("formatCode") == value:
            return int(node.attrib.get("numFmtId", "164"))
    used = {
        int(node.attrib.get("numFmtId", "0"))
        for node in formats.findall(f"{{{S}}}numFmt")
        if node.attrib.get("numFmtId", "").isdigit()
    }
    identifier = max({163, *used}) + 1
    formats.append(
        ET.Element(
            f"{{{S}}}numFmt",
            {"numFmtId": str(identifier), "formatCode": value},
        )
    )
    formats.set("count", str(len(formats)))
    return identifier


def _formatted_style_index(
    styles: ET.Element,
    *,
    base_index: int,
    plan: dict[str, object],
    cache: dict[tuple[int, str], int],
) -> int:
    style_plan = {
        key: value
        for key, value in plan.items()
        if key in {"font", "fill", "border", "alignment", "number_format"}
    }
    cache_key = (
        base_index,
        json.dumps(style_plan, sort_keys=True, separators=(",", ":")),
    )
    if cache_key in cache:
        return cache[cache_key]
    cell_xfs = _style_section(styles, "cellXfs")
    if not 0 <= base_index < len(cell_xfs):
        raise OoxmlMutationError(f"cell style index does not exist: {base_index}")
    base = cell_xfs[base_index]
    style = copy.deepcopy(base)
    if "font" in style_plan:
        fonts = _style_section(styles, "fonts")
        font_id = int(base.attrib.get("fontId", "0"))
        if not 0 <= font_id < len(fonts):
            raise OoxmlMutationError("base font style is missing")
        style.set(
            "fontId",
            str(
                _component_index(
                    fonts, _font_component(fonts[font_id], style_plan["font"])
                )
            ),
        )
        style.set("applyFont", "1")
    if "fill" in style_plan:
        fills = _style_section(styles, "fills")
        style.set(
            "fillId",
            str(_component_index(fills, _fill_component(style_plan["fill"]))),
        )
        style.set("applyFill", "1")
    if "border" in style_plan:
        borders = _style_section(styles, "borders")
        border_id = int(base.attrib.get("borderId", "0"))
        if not 0 <= border_id < len(borders):
            raise OoxmlMutationError("base border style is missing")
        style.set(
            "borderId",
            str(
                _component_index(
                    borders,
                    _border_component(borders[border_id], style_plan["border"]),
                )
            ),
        )
        style.set("applyBorder", "1")
    if "alignment" in style_plan:
        old = style.find(f"{{{S}}}alignment")
        if old is not None:
            style.remove(old)
        style.append(_alignment_component(base, style_plan["alignment"]))
        style.set("applyAlignment", "1")
    if "number_format" in style_plan:
        style.set(
            "numFmtId", str(_number_format_id(styles, style_plan["number_format"]))
        )
        style.set("applyNumberFormat", "1")
    index = _component_index(cell_xfs, style)
    cache[cache_key] = index
    return index


def _range_cells(value: object) -> list[tuple[str, int]]:
    if not isinstance(value, str):
        raise OoxmlMutationError("format range must be an A1 cell or rectangular range")
    match = CELL_RANGE.fullmatch(value.upper())
    if match is None:
        raise OoxmlMutationError(f"invalid A1 format range: {value}")
    start_column = _column_number(match.group(1))
    start_row = int(match.group(2))
    end_column = _column_number(match.group(3) or match.group(1))
    end_row = int(match.group(4) or match.group(2))
    if end_column < start_column or end_row < start_row:
        raise OoxmlMutationError(
            f"format range must run top-left to bottom-right: {value}"
        )
    count = (end_column - start_column + 1) * (end_row - start_row + 1)
    if count > 50_000:
        raise OoxmlMutationError("one format range may contain at most 50000 cells")

    def label(number: int) -> str:
        result = ""
        while number:
            number, remainder = divmod(number - 1, 26)
            result = chr(65 + remainder) + result
        return result

    return [
        (f"{label(column)}{row}", row)
        for row in range(start_row, end_row + 1)
        for column in range(start_column, end_column + 1)
    ]


def _sheet_view(root: ET.Element) -> ET.Element:
    views = root.find(f"{{{S}}}sheetViews")
    if views is None:
        views = ET.Element(f"{{{S}}}sheetViews")
        insertion = 0
        children = list(root)
        if children and children[0].tag == f"{{{S}}}sheetPr":
            insertion = 1
        root.insert(insertion, views)
    view = views.find(f"{{{S}}}sheetView")
    if view is None:
        view = ET.SubElement(views, f"{{{S}}}sheetView", {"workbookViewId": "0"})
    return view


def _apply_freeze_panes(root: ET.Element, value: object) -> None:
    if not isinstance(value, str) or CELL_REFERENCE.fullmatch(value.upper()) is None:
        raise OoxmlMutationError("freeze_panes must be an A1 cell such as A2 or B2")
    cell = value.upper()
    row, column = _cell_key(cell)
    x_split = column - 1
    y_split = row - 1
    view = _sheet_view(root)
    old = view.find(f"{{{S}}}pane")
    if old is not None:
        view.remove(old)
    if x_split == 0 and y_split == 0:
        return
    attributes = {"state": "frozen", "topLeftCell": cell}
    if x_split:
        attributes["xSplit"] = str(x_split)
    if y_split:
        attributes["ySplit"] = str(y_split)
    attributes["activePane"] = (
        "bottomRight"
        if x_split and y_split
        else "topRight"
        if x_split
        else "bottomLeft"
    )
    view.insert(0, ET.Element(f"{{{S}}}pane", attributes))


def _apply_column_settings(root: ET.Element, plans: object) -> int:
    if plans is None:
        return 0
    if not isinstance(plans, dict) or len(plans) > 256:
        raise OoxmlMutationError(
            "columns must map at most 256 column labels to settings"
        )
    columns = root.find(f"{{{S}}}cols")
    if columns is None:
        columns = ET.Element(f"{{{S}}}cols")
        sheet_data = root.find(f"{{{S}}}sheetData")
        index = list(root).index(sheet_data) if sheet_data is not None else len(root)
        root.insert(index, columns)
    changed = 0
    for label, plan in plans.items():
        if (
            not isinstance(label, str)
            or not re.fullmatch(r"[A-Za-z]{1,3}", label)
            or not isinstance(plan, dict)
            or set(plan) - {"width", "hidden"}
        ):
            raise OoxmlMutationError(
                "each columns entry needs a label and width/hidden"
            )
        number = _column_number(label.upper())
        source = next(
            (
                node
                for node in columns.findall(f"{{{S}}}col")
                if int(node.attrib.get("min", "0"))
                <= number
                <= int(node.attrib.get("max", "0"))
            ),
            None,
        )
        if source is None:
            node = ET.Element(f"{{{S}}}col", {"min": str(number), "max": str(number)})
            columns.append(node)
        else:
            minimum = int(source.attrib["min"])
            maximum = int(source.attrib["max"])
            insertion = list(columns).index(source)
            columns.remove(source)
            split: list[ET.Element] = []
            if minimum < number:
                before = copy.deepcopy(source)
                before.set("min", str(minimum))
                before.set("max", str(number - 1))
                split.append(before)
            node = copy.deepcopy(source)
            node.set("min", str(number))
            node.set("max", str(number))
            split.append(node)
            if number < maximum:
                after = copy.deepcopy(source)
                after.set("min", str(number + 1))
                after.set("max", str(maximum))
                split.append(after)
            for offset, item in enumerate(split):
                columns.insert(insertion + offset, item)
        if "width" in plan:
            width = plan["width"]
            if (
                isinstance(width, bool)
                or not isinstance(width, (int, float))
                or not math.isfinite(float(width))
                or not 0 <= float(width) <= 255
            ):
                raise OoxmlMutationError("column width must be from 0 through 255")
            node.set("width", str(width))
            node.set("customWidth", "1")
        if "hidden" in plan:
            if not isinstance(plan["hidden"], bool):
                raise OoxmlMutationError("column hidden must be true or false")
            node.set("hidden", "1" if plan["hidden"] else "0")
        changed += 1
    return changed


def _apply_row_settings(root: ET.Element, plans: object) -> int:
    if plans is None:
        return 0
    if not isinstance(plans, dict) or len(plans) > 10_000:
        raise OoxmlMutationError("rows must map at most 10000 row numbers to settings")
    sheet_data = root.find(f"{{{S}}}sheetData")
    if sheet_data is None:
        sheet_data = ET.SubElement(root, f"{{{S}}}sheetData")
    changed = 0
    for key, plan in plans.items():
        try:
            row_number = int(key)
        except (TypeError, ValueError):
            raise OoxmlMutationError("rows keys must be positive row numbers") from None
        if (
            row_number < 1
            or not isinstance(plan, dict)
            or set(plan) - {"height", "hidden"}
        ):
            raise OoxmlMutationError(
                "each rows entry needs a row number and height/hidden"
            )
        row = _find_or_create_row(sheet_data, row_number)
        if "height" in plan:
            height = plan["height"]
            if (
                isinstance(height, bool)
                or not isinstance(height, (int, float))
                or not math.isfinite(float(height))
                or not 0 <= float(height) <= 409
            ):
                raise OoxmlMutationError("row height must be from 0 through 409")
            row.set("ht", str(height))
            row.set("customHeight", "1")
        if "hidden" in plan:
            if not isinstance(plan["hidden"], bool):
                raise OoxmlMutationError("row hidden must be true or false")
            row.set("hidden", "1" if plan["hidden"] else "0")
        changed += 1
    return changed


def format_xlsx(
    source: Path,
    output: Path,
    *,
    plan: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Apply bounded workbook presentation changes without touching cell data."""

    if source.suffix.lower() not in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        raise OoxmlMutationError("workbook formatting requires an XLSX/XLSM package")
    if not plan or len(plan) > 128:
        raise OoxmlMutationError("format plan must name between 1 and 128 worksheets")
    replacements: dict[str, bytes] = {}
    changed_cells = 0
    changed_ranges = 0
    changed_columns = 0
    changed_rows = 0
    with _validated_archive(source) as archive:
        names = archive.namelist()
        if "xl/styles.xml" not in names:
            raise OoxmlMutationError("workbook has no styles part to extend surgically")
        styles = _xml(archive.read("xl/styles.xml"), "xl/styles.xml")
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
        sheet_nodes = {
            node.attrib.get("name", ""): node
            for node in workbook.findall(".//s:sheet", NS)
        }
        sheet_parts = {
            name: targets.get(node.attrib.get(f"{{{R}}}id", ""))
            for name, node in sheet_nodes.items()
        }
        style_cache: dict[tuple[int, str], int] = {}
        workbook_changed = False
        for sheet_name, sheet_plan in plan.items():
            if not isinstance(sheet_name, str) or not isinstance(sheet_plan, dict):
                raise OoxmlMutationError("format plan must map sheet names to objects")
            unknown = set(sheet_plan) - {
                "ranges",
                "columns",
                "rows",
                "freeze_panes",
                "show_gridlines",
                "tab_color",
                "state",
            }
            if unknown:
                raise OoxmlMutationError(
                    f"unsupported {sheet_name} format fields: "
                    + ", ".join(sorted(unknown))
                )
            part = sheet_parts.get(sheet_name)
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
            ranges = sheet_plan.get("ranges", [])
            if not isinstance(ranges, list) or len(ranges) > 512:
                raise OoxmlMutationError("ranges must contain at most 512 format rules")
            for rule in ranges:
                if not isinstance(rule, dict) or "range" not in rule:
                    raise OoxmlMutationError("each format rule requires range")
                allowed = {
                    "range",
                    "font",
                    "fill",
                    "border",
                    "alignment",
                    "number_format",
                    "copy_style_from",
                    "apply_to_blank_cells",
                }
                if set(rule) - allowed:
                    raise OoxmlMutationError(
                        "unsupported range format fields: "
                        + ", ".join(sorted(set(rule) - allowed))
                    )
                if not any(
                    key in rule
                    for key in (
                        "font",
                        "fill",
                        "border",
                        "alignment",
                        "number_format",
                        "copy_style_from",
                    )
                ):
                    raise OoxmlMutationError(
                        "a format rule must change at least one style property"
                    )
                apply_blanks = rule.get("apply_to_blank_cells", False)
                if not isinstance(apply_blanks, bool):
                    raise OoxmlMutationError(
                        "apply_to_blank_cells must be true or false"
                    )
                copy_from = rule.get("copy_style_from")
                copied_style: int | None = None
                if copy_from is not None:
                    if not isinstance(copy_from, str):
                        raise OoxmlMutationError("copy_style_from must be an A1 cell")
                    source_cell = existing_cells.get(copy_from.upper())
                    if source_cell is None:
                        raise OoxmlMutationError(
                            f"style source {copy_from} does not exist on {sheet_name}"
                        )
                    copied_style = int(source_cell.attrib.get("s", "0"))
                for reference, row_number in _range_cells(rule["range"]):
                    cell = existing_cells.get(reference)
                    if cell is None and not apply_blanks:
                        continue
                    if cell is None:
                        row = _find_or_create_row(sheet_data, row_number)
                        cell = _find_or_create_cell(row, reference)
                        existing_cells[reference] = cell
                    base_index = (
                        copied_style
                        if copied_style is not None
                        else int(cell.attrib.get("s", "0"))
                    )
                    cell.set(
                        "s",
                        str(
                            _formatted_style_index(
                                styles,
                                base_index=base_index,
                                plan=rule,
                                cache=style_cache,
                            )
                        ),
                    )
                    changed_cells += 1
                changed_ranges += 1
            changed_columns += _apply_column_settings(root, sheet_plan.get("columns"))
            changed_rows += _apply_row_settings(root, sheet_plan.get("rows"))
            if "freeze_panes" in sheet_plan:
                _apply_freeze_panes(root, sheet_plan["freeze_panes"])
            if "show_gridlines" in sheet_plan:
                value = sheet_plan["show_gridlines"]
                if not isinstance(value, bool):
                    raise OoxmlMutationError("show_gridlines must be true or false")
                _sheet_view(root).set("showGridLines", "1" if value else "0")
            if "tab_color" in sheet_plan:
                sheet_properties = root.find(f"{{{S}}}sheetPr")
                if sheet_properties is None:
                    sheet_properties = ET.Element(f"{{{S}}}sheetPr")
                    root.insert(0, sheet_properties)
                old = sheet_properties.find(f"{{{S}}}tabColor")
                if old is not None:
                    sheet_properties.remove(old)
                sheet_properties.insert(
                    0,
                    ET.Element(
                        f"{{{S}}}tabColor",
                        {"rgb": _style_color(sheet_plan["tab_color"], "tab_color")},
                    ),
                )
            if "state" in sheet_plan:
                state = sheet_plan["state"]
                if state not in {"visible", "hidden", "veryHidden"}:
                    raise OoxmlMutationError(
                        "state must be visible, hidden, or veryHidden"
                    )
                node = sheet_nodes[sheet_name]
                if state == "visible":
                    node.attrib.pop("state", None)
                else:
                    node.set("state", str(state))
                workbook_changed = True
            replacements[part] = _serialized(root)
        replacements["xl/styles.xml"] = _serialized(styles)
        if workbook_changed:
            replacements["xl/workbook.xml"] = _serialized(workbook)
    _rewrite_package(source, output, replacements)
    return _mutation_result(
        "format_xlsx",
        source,
        output,
        details={
            "changed_range_count": changed_ranges,
            "styled_cell_count": changed_cells,
            "changed_column_count": changed_columns,
            "changed_row_count": changed_rows,
            "changed_parts": sorted(replacements),
        },
    )


def load_format_plan(path: Path) -> dict[str, dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OoxmlMutationError(
            f"format plan must be valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(sheet, str) and isinstance(plan, dict)
        for sheet, plan in value.items()
    ):
        raise OoxmlMutationError("format plan must map worksheet names to objects")
    return value


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
