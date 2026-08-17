from __future__ import annotations

import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile


SOURCE_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(SOURCE_ROOT / "python"))

from neoharness_office.images import (  # noqa: E402
    auto_orient,
    crop,
    crop_to_content,
    fit,
    flatten,
    image_info,
)
from neoharness_office.inspectors import compare_files, inspect_file  # noqa: E402
from neoharness_office.ocr import make_searchable_pdf  # noqa: E402
from neoharness_office.ooxml import (  # noqa: E402
    replace_text,
    replace_text_with_field,
    update_content_control,
    update_xlsx_cells,
)
from neoharness_office.pdf_ops import (  # noqa: E402
    extract as extract_pdf,
    merge as merge_pdf,
    rotate as rotate_pdf,
)
from neoharness_office.paths import exact_read_file  # noqa: E402
from neoharness_office.quality import (  # noqa: E402
    QualityPolicyError,
    artifact_provenance,
    policy_identity,
    qualify_artifact,
)
from neoharness_office.rendering import (  # noqa: E402
    before_after,
    render_page,
    render_pdf,
)


CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>
"""


class DocumentHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _xlsx(self, path: Path, *, changed: bool = False) -> None:
        workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="Free form" sheetId="1" r:id="rId1"/></sheets>
          <definedNames><definedName name="Print_Area">'Free form'!$A$1:$F$8</definedName></definedNames>
        </workbook>"""
        rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
        </Relationships>"""
        marker = "2" if changed else "1"
        sheet = f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <dimension ref="A1:F8"/><sheetData>
            <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>{marker}</v></c></row>
            <row r="2"><c r="A2"><f>B1*2</f><v>{int(marker) * 2}</v></c></row>
            <row r="8"><c r="F8" t="inlineStr"><is><t>separate note</t></is></c></row>
          </sheetData>
          <mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>
          <conditionalFormatting sqref="A1:B2"/>
          <dataValidations count="1"><dataValidation sqref="B1"/></dataValidations>
          <drawing r:id="rId2" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
        </worksheet>"""
        shared = """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Heading</t></si></sst>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", rels)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
            archive.writestr("xl/sharedStrings.xml", shared)

    def _docx(self, path: Path, *, macro: bytes | None = None) -> None:
        document = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
          xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
          <w:body>
            <w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Actual starting</w:t></w:r><w:r><w:t xml:space="preserve"> state</w:t></w:r><w:instrText>DATE \\@ yyyy-MM-dd</w:instrText></w:p>
            <w:sdt><w:sdtPr><w:tag w:val="customer"/></w:sdtPr><w:sdtContent><w:p><w:r><w:t>Controlled</w:t></w:r></w:p></w:sdtContent></w:sdt>
            <w:tbl><w:tr><w:tc><w:p/></w:tc><w:tc><w:p/></w:tc></w:tr></w:tbl>
            <w:ins><w:r><w:t>Tracked</w:t></w:r></w:ins><w:sectPr/>
          </w:body>
        </w:document>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("word/document.xml", document)
            archive.writestr(
                "word/_rels/document.xml.rels",
                """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                <Relationship Id="rId9" Type="hyperlink" Target="https://example.invalid" TargetMode="External"/>
                </Relationships>""",
            )
            archive.writestr(
                "word/settings.xml",
                """<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>""",
            )
            if macro is not None:
                archive.writestr("word/vbaProject.bin", macro)

    def test_read_boundary_skips_an_unused_missing_workspace_root(self) -> None:
        existing = self.root / "output"
        existing.mkdir()
        artifact = existing / "artifact.pdf"
        artifact.write_bytes(b"artifact")
        self.assertEqual(
            exact_read_file(
                artifact,
                roots=(self.root / "missing-input", existing),
            ),
            artifact,
        )

    def test_xlsx_inventory_treats_sheet_as_spatial_artifact(self) -> None:
        path = self.root / "irregular.xlsx"
        self._xlsx(path)
        result = inspect_file(path)
        semantic = result["inspection"]["semantic"]
        sheet = semantic["sheets"][0]
        self.assertEqual(semantic["sheet_count"], 1)
        self.assertEqual(sheet["formula_cells"], 1)
        self.assertEqual(sheet["merged_range_count"], 1)
        self.assertEqual(sheet["data_validation_count"], 1)
        self.assertEqual(sheet["conditional_format_count"], 1)
        self.assertEqual(sheet["drawing_count"], 1)
        self.assertEqual(len(sheet["regions"]), 2)
        self.assertEqual(sheet["sample_cells"][0]["value"], "Heading")

    def test_docx_inventory_reports_structure_and_external_authority(self) -> None:
        path = self.root / "structured.docm"
        self._docx(path, macro=b"macro-bytes")
        result = inspect_file(path)
        package = result["inspection"]["package"]
        semantic = result["inspection"]["semantic"]
        self.assertTrue(package["macro_bearing"])
        self.assertEqual(result["inspection"]["relationships"]["external_count"], 1)
        self.assertEqual(semantic["table_count"], 1)
        self.assertEqual(semantic["field_instruction_count"], 1)
        self.assertEqual(semantic["content_control_count"], 1)
        self.assertEqual(semantic["tracked_insert_count"], 1)
        self.assertIn("Actual starting state", semantic["text_sample"])

    def test_package_comparison_reports_changed_part_and_macro_preservation(self) -> None:
        before = self.root / "before.xlsm"
        after = self.root / "after.xlsm"
        self._xlsx(before)
        self._xlsx(after, changed=True)
        for path in (before, after):
            with zipfile.ZipFile(path, "a", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("xl/vbaProject.bin", b"same-macro")
        result = compare_files(before, after)
        package = result["package"]
        self.assertIn("xl/worksheets/sheet1.xml", package["changed_parts"])
        self.assertTrue(package["macro_parts"][0]["preserved"])

    def test_surgical_docx_operations_preserve_macro_and_target_exact_state(self) -> None:
        source = self.root / "source.docm"
        replaced = self.root / "replaced.docm"
        controlled = self.root / "controlled.docm"
        field = self.root / "field.docm"
        self._docx(source, macro=b"macro-bytes")

        result = replace_text(
            source,
            replaced,
            search="Actual starting state",
            replacement="Observed starting state",
            expected_count=1,
        )
        self.assertEqual(result["details"]["matches"], 1)
        self.assertEqual(
            result["provenance"]["finalizer"]["class"], "surgical_ooxml"
        )
        qualification = qualify_artifact(
            replaced,
            intent="revise",
            provenance=result["provenance"],
        )
        self.assertEqual(qualification["status"], "qualified")
        update_content_control(
            replaced,
            controlled,
            tag="customer",
            value="Acme Bakery",
            expected_count=1,
        )
        field_result = replace_text_with_field(
            controlled,
            field,
            search="Observed starting state",
            instruction='DATE \\@ "MMMM d, yyyy"',
            display="August 17, 2026",
            expected_count=1,
        )
        self.assertEqual(field_result["details"]["matches"], 1)
        with zipfile.ZipFile(field) as archive:
            document = archive.read("word/document.xml")
            settings = archive.read("word/settings.xml")
            self.assertIn(b"Acme Bakery", document)
            self.assertIn(b"fldCharType=\"begin\"", document)
            self.assertIn(b"DATE", document)
            self.assertIn(b"updateFields", settings)
            self.assertEqual(archive.read("word/vbaProject.bin"), b"macro-bytes")

    def test_surgical_xlsx_updates_disconnected_cells_without_flattening_package(self) -> None:
        source = self.root / "source.xlsm"
        output = self.root / "output.xlsm"
        self._xlsx(source)
        with zipfile.ZipFile(source, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("xl/vbaProject.bin", b"macro-bytes")
        result = update_xlsx_cells(
            source,
            output,
            updates={
                "Free form": {
                    "B2": {"value": 41, "style_from": "B1"},
                    "C7": {"formula": "=B2+1", "cached": 42},
                    "F8": {"value": "preserved spatial note"},
                }
            },
        )
        self.assertEqual(result["details"]["changed_cell_count"], 3)
        with zipfile.ZipFile(output) as archive:
            sheet = archive.read("xl/worksheets/sheet1.xml")
            workbook = archive.read("xl/workbook.xml")
            self.assertIn(b"preserved spatial note", sheet)
            self.assertIn(b"B2+1", sheet)
            self.assertIn(b"fullCalcOnLoad=\"1\"", workbook)
            self.assertEqual(archive.read("xl/vbaProject.bin"), b"macro-bytes")

    @unittest.skipUnless(shutil.which("vips") and shutil.which("convert"), "image tools")
    def test_image_operations_preserve_exact_outputs_and_dimensions(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        source = self.root / "source.png"
        image = Image.new("RGBA", (200, 120), "white")
        drawing = ImageDraw.Draw(image)
        drawing.rectangle((20, 20, 179, 99), fill="green")
        image.save(source)

        cropped = self.root / "cropped.png"
        content = self.root / "content.png"
        contained = self.root / "contained.png"
        covered = self.root / "covered.png"
        oriented = self.root / "oriented.png"
        flattened = self.root / "flattened.jpg"
        crop_result = crop(source, cropped, left=20, top=20, width=160, height=80)
        crop_to_content(source, content)
        fit(source, contained, width=100, height=100, mode="contain")
        fit(source, covered, width=100, height=100, mode="cover")
        auto_orient(source, oriented)
        flatten(source, flattened)

        with Image.open(cropped) as result:
            self.assertEqual(result.size, (160, 80))
        with Image.open(content) as result:
            self.assertEqual(result.size, (160, 80))
        with Image.open(contained) as result:
            self.assertEqual(result.size, (100, 100))
        with Image.open(covered) as result:
            self.assertEqual(result.size, (100, 100))
        with Image.open(oriented) as result:
            self.assertEqual(result.size, (200, 120))
        with Image.open(flattened) as result:
            self.assertEqual(result.mode, "RGB")
        self.assertEqual(image_info(cropped)["pillow"]["width"], 160)

        self.assertEqual(
            crop_result["provenance"]["finalizer"]["class"], "image_transform"
        )
        self.assertEqual(
            qualify_artifact(
                cropped,
                intent="create",
                provenance=crop_result["provenance"],
            )["status"],
            "qualified",
        )

    @unittest.skipUnless(
        shutil.which("img2pdf")
        and shutil.which("pdftoppm")
        and shutil.which("qpdf")
        and shutil.which("tesseract"),
        "render, PDF, and OCR tools",
    )
    def test_image_pdf_render_ocr_and_page_operations(self) -> None:
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is not installed")
        source = self.root / "source.png"
        image = Image.new("RGB", (640, 240), "white")
        drawing = ImageDraw.Draw(image)
        drawing.text((40, 80), "Invoice 12345", fill="black")
        image.save(source)

        first_pdf = self.root / "first.pdf"
        second_pdf = self.root / "second.pdf"
        merged = self.root / "merged.pdf"
        extracted = self.root / "extracted.pdf"
        rotated = self.root / "rotated.pdf"
        page = self.root / "page.png"
        comparison = self.root / "comparison.png"
        searchable = self.root / "searchable.pdf"
        rendered = render_pdf(source, first_pdf)
        render_pdf(source, second_pdf)
        merge_pdf([first_pdf, second_pdf], merged)
        extract_pdf(merged, extracted, pages="2")
        rotate_pdf(extracted, rotated, degrees=90)
        render_page(rotated, page, page=1, dpi=72)
        before_after(first_pdf, rotated, comparison, page=1, dpi=72)
        ocr_result = make_searchable_pdf(source, searchable)
        for artifact in (
            first_pdf,
            merged,
            extracted,
            rotated,
            page,
            comparison,
            searchable,
        ):
            self.assertGreater(artifact.stat().st_size, 0)
        self.assertEqual(
            rendered["provenance"]["finalizer"]["class"], "pdf_render"
        )
        self.assertEqual(
            ocr_result["provenance"]["finalizer"]["class"], "pdf_ocr"
        )

    def test_quality_policy_rejects_unapproved_or_mismatched_evidence(self) -> None:
        artifact = self.root / "candidate.docx"
        artifact.write_bytes(b"candidate")
        provenance = artifact_provenance(
            artifact,
            finalizer="unqualified",
            producer="libreoffice",
            operation="save",
        )
        with self.assertRaisesRegex(
            QualityPolicyError, "cannot qualify revise/ooxml"
        ):
            qualify_artifact(
                artifact,
                intent="revise",
                provenance=provenance,
            )

        approved = artifact_provenance(
            artifact,
            finalizer="native_office",
            producer="docbuilder",
            operation="run",
        )
        artifact.write_bytes(b"changed-after-finalization")
        with self.assertRaisesRegex(QualityPolicyError, "does not match"):
            qualify_artifact(
                artifact,
                intent="revise",
                provenance=approved,
            )

        identity = policy_identity()
        self.assertEqual(identity["version"], "neoharness-office-quality.v1")
        self.assertEqual(len(identity["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
