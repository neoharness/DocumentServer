from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from .images import (
    auto_orient,
    convert,
    crop,
    crop_to_content,
    fit,
    flatten,
    image_info,
)
from .inspectors import compare_files, inspect_file
from .ocr import make_searchable_pdf
from .ooxml import (
    load_update_plan,
    replace_text,
    replace_text_with_field,
    update_content_control,
    update_xlsx_cells,
)
from .paths import WorkspaceContractError, exact_read_file, exact_write_file
from .pdf_ops import extract as extract_pdf
from .pdf_ops import linearize as linearize_pdf
from .pdf_ops import merge as merge_pdf
from .pdf_ops import rotate as rotate_pdf
from .process import UtilityError
from .quality import (
    find_artifact_provenance,
    policy_identity,
    qualify_artifact,
)
from .rendering import before_after, render_page, render_pdf


EXIT_DATA = 65
EXIT_TOOL = 70
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def _emit(value: object, output: str | None) -> None:
    encoded = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if output is None:
        sys.stdout.write(encoded)
        return
    path = exact_write_file(output)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(encoded)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
    sys.stdout.write(json.dumps({"output": str(path), "status": "succeeded"}) + "\n")


def _inspect(args: argparse.Namespace) -> int:
    paths = [exact_read_file(item) for item in args.files]
    inspections = [inspect_file(path) for path in paths]
    value: object = inspections[0] if len(inspections) == 1 else {
        "schema": "ai.neoharness.office.workspace-inspection.v1",
        "files": inspections,
    }
    _emit(value, args.output)
    return 0


def _compare(args: argparse.Namespace) -> int:
    _emit(
        compare_files(exact_read_file(args.before), exact_read_file(args.after)),
        args.output,
    )
    return 0


def _image(args: argparse.Namespace) -> int:
    input_path = exact_read_file(args.input)
    if args.image_action == "info":
        _emit(
            {
                "schema": "ai.neoharness.office.image-inspection.v1",
                "image": image_info(input_path),
            },
            args.json_output,
        )
        return 0

    output_path = exact_write_file(args.output)
    handlers: dict[str, Any] = {
        "crop": lambda: crop(
            input_path,
            output_path,
            left=args.left,
            top=args.top,
            width=args.width,
            height=args.height,
        ),
        "crop-content": lambda: crop_to_content(
            input_path, output_path, fuzz_percent=args.fuzz
        ),
        "fit": lambda: fit(
            input_path,
            output_path,
            width=args.width,
            height=args.height,
            mode=args.mode,
            background=args.background,
        ),
        "orient": lambda: auto_orient(input_path, output_path),
        "convert": lambda: convert(input_path, output_path),
        "flatten": lambda: flatten(
            input_path, output_path, background=args.background
        ),
    }
    _emit(
        {
            "schema": "ai.neoharness.office.image-operation.v1",
            "result": handlers[args.image_action](),
        },
        args.json_output,
    )
    return 0


def _render(args: argparse.Namespace) -> int:
    if args.render_action == "pdf":
        result = render_pdf(
            exact_read_file(args.input), exact_write_file(args.output)
        )
    elif args.render_action == "page":
        result = render_page(
            exact_read_file(args.input),
            exact_write_file(args.output),
            page=args.page,
            dpi=args.dpi,
        )
    else:
        result = before_after(
            exact_read_file(args.before),
            exact_read_file(args.after),
            exact_write_file(args.output),
            page=args.page,
            dpi=args.dpi,
        )
    _emit(
        {"schema": "ai.neoharness.office.render-operation.v1", "result": result},
        args.json_output,
    )
    return 0


def _ocr(args: argparse.Namespace) -> int:
    result = make_searchable_pdf(
        exact_read_file(args.input),
        exact_write_file(args.output),
        languages=args.languages,
        force=args.force,
        deskew=not args.no_deskew,
        rotate_pages=not args.no_rotate,
    )
    _emit(
        {"schema": "ai.neoharness.office.ocr-operation.v1", "result": result},
        args.json_output,
    )
    return 0


def _pdf(args: argparse.Namespace) -> int:
    output = exact_write_file(args.output)
    if args.pdf_action == "merge":
        result = merge_pdf([exact_read_file(item) for item in args.inputs], output)
    elif args.pdf_action == "extract":
        result = extract_pdf(
            exact_read_file(args.input), output, pages=args.pages
        )
    elif args.pdf_action == "rotate":
        result = rotate_pdf(
            exact_read_file(args.input),
            output,
            degrees=args.degrees,
            pages=args.pages,
        )
    else:
        result = linearize_pdf(exact_read_file(args.input), output)
    _emit(
        {"schema": "ai.neoharness.office.pdf-operation.v1", "result": result},
        args.json_output,
    )
    return 0


def _ooxml(args: argparse.Namespace) -> int:
    source = exact_read_file(args.input)
    output = exact_write_file(args.output)
    if args.ooxml_action == "replace-text":
        result = replace_text(
            source,
            output,
            search=args.search,
            replacement=args.replacement,
            match_case=not args.ignore_case,
            expected_count=args.expected_count,
            maximum_count=args.maximum_count,
        )
    elif args.ooxml_action == "word-field":
        result = replace_text_with_field(
            source,
            output,
            search=args.search,
            instruction=args.instruction,
            display=args.display,
            match_case=not args.ignore_case,
            expected_count=args.expected_count,
        )
    elif args.ooxml_action == "content-control":
        result = update_content_control(
            source,
            output,
            tag=args.tag,
            value=args.value,
            expected_count=args.expected_count,
        )
    else:
        plan = load_update_plan(exact_read_file(args.plan))
        result = update_xlsx_cells(source, output, updates=plan)
    _emit(
        {"schema": "ai.neoharness.office.surgical-operation.v1", "result": result},
        args.json_output,
    )
    return 0


def _quality(args: argparse.Namespace) -> int:
    if args.quality_action == "identity":
        _emit(
            {
                "schema": "ai.neoharness.office.quality-policy-identity.v1",
                "policy": policy_identity(),
            },
            args.json_output,
        )
        return 0

    artifact = exact_read_file(args.artifact)
    evidence_path = exact_read_file(args.evidence)
    if evidence_path.stat().st_size > MAX_EVIDENCE_BYTES:
        raise ValueError("quality evidence exceeds the 16 MiB local limit")
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"quality evidence must be valid UTF-8 JSON: {exc}") from exc
    provenance = find_artifact_provenance(evidence, artifact)
    result = qualify_artifact(
        artifact,
        intent=args.intent,
        family=None if args.family == "auto" else args.family,
        provenance=provenance,
    )
    _emit(result, args.json_output)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nh-document",
        description=(
            "Inspect and manipulate exact files inside a neoHarness document workspace."
        ),
    )
    parser.add_argument("--version", action="version", version="%(prog)s 9.3.3-nh1")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser(
        "inspect", help="inventory actual document structure before making assumptions"
    )
    inspect_parser.add_argument("files", nargs="+")
    inspect_parser.add_argument("--output", help="write JSON beneath work or output")
    inspect_parser.set_defaults(handler=_inspect)

    compare_parser = subparsers.add_parser(
        "compare", help="compare exact package parts and preservation facts"
    )
    compare_parser.add_argument("before")
    compare_parser.add_argument("after")
    compare_parser.add_argument("--output", help="write JSON beneath work or output")
    compare_parser.set_defaults(handler=_compare)

    image_parser = subparsers.add_parser(
        "image", help="inspect, crop, orient, resize, or convert an image"
    )
    image_actions = image_parser.add_subparsers(dest="image_action", required=True)

    info = image_actions.add_parser("info")
    info.add_argument("input")
    info.add_argument("--json-output")
    info.set_defaults(handler=_image)

    crop_parser = image_actions.add_parser("crop")
    crop_parser.add_argument("input")
    crop_parser.add_argument("output")
    crop_parser.add_argument("--left", type=int, required=True)
    crop_parser.add_argument("--top", type=int, required=True)
    crop_parser.add_argument("--width", type=int, required=True)
    crop_parser.add_argument("--height", type=int, required=True)
    crop_parser.add_argument("--json-output")
    crop_parser.set_defaults(handler=_image)

    crop_content = image_actions.add_parser("crop-content")
    crop_content.add_argument("input")
    crop_content.add_argument("output")
    crop_content.add_argument("--fuzz", type=float, default=0.0)
    crop_content.add_argument("--json-output")
    crop_content.set_defaults(handler=_image)

    fit_parser = image_actions.add_parser("fit")
    fit_parser.add_argument("input")
    fit_parser.add_argument("output")
    fit_parser.add_argument("--width", type=int, required=True)
    fit_parser.add_argument("--height", type=int, required=True)
    fit_parser.add_argument("--mode", choices=("contain", "cover"), required=True)
    fit_parser.add_argument("--background", default="none")
    fit_parser.add_argument("--json-output")
    fit_parser.set_defaults(handler=_image)

    for name in ("orient", "convert", "flatten"):
        action = image_actions.add_parser(name)
        action.add_argument("input")
        action.add_argument("output")
        if name == "flatten":
            action.add_argument("--background", default="white")
        action.add_argument("--json-output")
        action.set_defaults(handler=_image)

    render_parser = subparsers.add_parser(
        "render", help="render documents or exact before/after pages"
    )
    render_actions = render_parser.add_subparsers(dest="render_action", required=True)

    render_pdf_parser = render_actions.add_parser("pdf")
    render_pdf_parser.add_argument("input")
    render_pdf_parser.add_argument("output")
    render_pdf_parser.add_argument("--json-output")
    render_pdf_parser.set_defaults(handler=_render)

    render_page_parser = render_actions.add_parser("page")
    render_page_parser.add_argument("input")
    render_page_parser.add_argument("output")
    render_page_parser.add_argument("--page", type=int, default=1)
    render_page_parser.add_argument("--dpi", type=int, default=144)
    render_page_parser.add_argument("--json-output")
    render_page_parser.set_defaults(handler=_render)

    render_compare_parser = render_actions.add_parser("compare")
    render_compare_parser.add_argument("before")
    render_compare_parser.add_argument("after")
    render_compare_parser.add_argument("output")
    render_compare_parser.add_argument("--page", type=int, default=1)
    render_compare_parser.add_argument("--dpi", type=int, default=120)
    render_compare_parser.add_argument("--json-output")
    render_compare_parser.set_defaults(handler=_render)

    ocr_parser = subparsers.add_parser(
        "ocr", help="add a searchable text layer to a PDF or image"
    )
    ocr_parser.add_argument("input")
    ocr_parser.add_argument("output")
    ocr_parser.add_argument("--languages", default="eng")
    ocr_parser.add_argument("--force", action="store_true")
    ocr_parser.add_argument("--no-deskew", action="store_true")
    ocr_parser.add_argument("--no-rotate", action="store_true")
    ocr_parser.add_argument("--json-output")
    ocr_parser.set_defaults(handler=_ocr)

    pdf_parser = subparsers.add_parser(
        "pdf", help="merge, extract, rotate, or linearize PDF pages"
    )
    pdf_actions = pdf_parser.add_subparsers(dest="pdf_action", required=True)
    pdf_merge_parser = pdf_actions.add_parser("merge")
    pdf_merge_parser.add_argument("inputs", nargs="+")
    pdf_merge_parser.add_argument("--output", required=True)
    pdf_merge_parser.add_argument("--json-output")
    pdf_merge_parser.set_defaults(handler=_pdf)
    pdf_extract_parser = pdf_actions.add_parser("extract")
    pdf_extract_parser.add_argument("input")
    pdf_extract_parser.add_argument("output")
    pdf_extract_parser.add_argument("--pages", required=True)
    pdf_extract_parser.add_argument("--json-output")
    pdf_extract_parser.set_defaults(handler=_pdf)
    pdf_rotate_parser = pdf_actions.add_parser("rotate")
    pdf_rotate_parser.add_argument("input")
    pdf_rotate_parser.add_argument("output")
    pdf_rotate_parser.add_argument("--degrees", type=int, required=True)
    pdf_rotate_parser.add_argument("--pages", default="1-z")
    pdf_rotate_parser.add_argument("--json-output")
    pdf_rotate_parser.set_defaults(handler=_pdf)
    pdf_linearize_parser = pdf_actions.add_parser("linearize")
    pdf_linearize_parser.add_argument("input")
    pdf_linearize_parser.add_argument("output")
    pdf_linearize_parser.add_argument("--json-output")
    pdf_linearize_parser.set_defaults(handler=_pdf)

    ooxml_parser = subparsers.add_parser(
        "ooxml", help="make bounded surgical OOXML changes while preserving other parts"
    )
    ooxml_actions = ooxml_parser.add_subparsers(dest="ooxml_action", required=True)
    replace_parser = ooxml_actions.add_parser("replace-text")
    replace_parser.add_argument("input")
    replace_parser.add_argument("output")
    replace_parser.add_argument("--search", required=True)
    replace_parser.add_argument("--replacement", required=True)
    replace_parser.add_argument("--ignore-case", action="store_true")
    replace_parser.add_argument("--expected-count", type=int)
    replace_parser.add_argument("--maximum-count", type=int)
    replace_parser.add_argument("--json-output")
    replace_parser.set_defaults(handler=_ooxml)

    field_parser = ooxml_actions.add_parser("word-field")
    field_parser.add_argument("input")
    field_parser.add_argument("output")
    field_parser.add_argument("--search", required=True)
    field_parser.add_argument("--instruction", required=True)
    field_parser.add_argument("--display")
    field_parser.add_argument("--ignore-case", action="store_true")
    field_parser.add_argument("--expected-count", type=int, default=1)
    field_parser.add_argument("--json-output")
    field_parser.set_defaults(handler=_ooxml)

    control_parser = ooxml_actions.add_parser("content-control")
    control_parser.add_argument("input")
    control_parser.add_argument("output")
    control_parser.add_argument("--tag", required=True)
    control_parser.add_argument("--value", required=True)
    control_parser.add_argument("--expected-count", type=int)
    control_parser.add_argument("--json-output")
    control_parser.set_defaults(handler=_ooxml)

    cells_parser = ooxml_actions.add_parser("xlsx-cells")
    cells_parser.add_argument("input")
    cells_parser.add_argument("output")
    cells_parser.add_argument("--plan", required=True)
    cells_parser.add_argument("--json-output")
    cells_parser.set_defaults(handler=_ooxml)

    quality_parser = subparsers.add_parser(
        "quality",
        help="inspect the canonical quality policy or qualify exact finalizer evidence",
    )
    quality_actions = quality_parser.add_subparsers(
        dest="quality_action", required=True
    )
    quality_identity = quality_actions.add_parser("identity")
    quality_identity.add_argument("--json-output")
    quality_identity.set_defaults(handler=_quality)
    quality_qualify = quality_actions.add_parser("qualify")
    quality_qualify.add_argument("artifact")
    quality_qualify.add_argument("--evidence", required=True)
    quality_qualify.add_argument(
        "--intent", choices=("create", "revise", "derived"), required=True
    )
    quality_qualify.add_argument(
        "--family", choices=("auto", "ooxml", "pdf", "image"), default="auto"
    )
    quality_qualify.add_argument("--json-output")
    quality_qualify.set_defaults(handler=_quality)

    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        return int(args.handler(args))
    except (WorkspaceContractError, ValueError) as exc:
        print(f"nh-document: {exc}", file=sys.stderr)
        return EXIT_DATA
    except UtilityError as exc:
        print(f"nh-document: {exc}", file=sys.stderr)
        return EXIT_TOOL


if __name__ == "__main__":
    raise SystemExit(main())
