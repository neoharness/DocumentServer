from __future__ import annotations

import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

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
from neoharness_office.inspectors import (  # noqa: E402
    _translate_formula,
    compare_files,
    inspect_file,
)
from neoharness_office.ocr import make_searchable_pdf  # noqa: E402
from neoharness_office.ooxml import (  # noqa: E402
    OoxmlMutationError,
    format_xlsx,
    replace_text,
    replace_text_with_field,
    update_content_control,
    update_xlsx_cells,
)
from neoharness_office.paths import exact_read_file  # noqa: E402
from neoharness_office.pdf_ops import (  # noqa: E402
    extract as extract_pdf,
)
from neoharness_office.pdf_ops import (
    merge as merge_pdf,
)
from neoharness_office.pdf_ops import (
    rotate as rotate_pdf,
)
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

    def _xlsx(
        self,
        path: Path,
        *,
        changed: bool = False,
        formula: str = "B1*2",
        sheet_state: str = "visible",
        defined_name_value: str = "'Free form'!$A$1:$F$8",
        extra_parts: dict[str, bytes] | None = None,
    ) -> None:
        workbook = f"""<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="Free form" sheetId="1" state="{sheet_state}" r:id="rId1"/></sheets>
          <definedNames><definedName name="Print_Area">{defined_name_value}</definedName></definedNames>
        </workbook>"""
        rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
        </Relationships>"""
        marker = "2" if changed else "1"
        sheet = f"""<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
          mc:Ignorable="x14ac xr2"
          xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac"
          xmlns:xr2="http://schemas.microsoft.com/office/spreadsheetml/2015/revision2">
          <dimension ref="A1:F8"/><sheetData>
            <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>{marker}</v></c></row>
            <row r="2"><c r="A2"><f>{formula}</f><v>{int(marker) * 2}</v></c></row>
            <row r="8"><c r="F8" t="inlineStr"><is><t>separate note</t></is></c></row>
          </sheetData>
          <mergeCells count="1"><mergeCell ref="A1:B1"/></mergeCells>
          <conditionalFormatting sqref="A1:B2"/>
          <dataValidations count="1"><dataValidation sqref="B1"/></dataValidations>
          <drawing r:id="rId2" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
        </worksheet>"""
        shared = """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>Heading</t></si></sst>"""
        styles = """<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
          <fonts count="1"><font><name val="Aptos"/><sz val="11"/></font></fonts>
          <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
          <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
          <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
          <cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>
        </styleSheet>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", rels)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
            archive.writestr("xl/sharedStrings.xml", shared)
            archive.writestr("xl/styles.xml", styles)
            for name, data in (extra_parts or {}).items():
                archive.writestr(name, data)

    def _shared_formula_xlsx(self, path: Path, cells_xml: str) -> None:
        """Minimal workbook whose only sheet carries the given formula cells."""

        workbook = """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
          xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
          <sheets><sheet name="Calc" sheetId="1" r:id="rId1"/></sheets>
        </workbook>"""
        rels = """<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
          <Relationship Id="rId1" Type="worksheet" Target="worksheets/sheet1.xml"/>
        </Relationships>"""
        sheet = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{cells_xml}</sheetData></worksheet>"
        )
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", rels)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)

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

    def test_docx_inventory_counts_body_header_and_footer_drawings(self) -> None:
        path = self.root / "branded.docx"
        document = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:drawing/></w:r></w:p></w:body>
        </w:document>"""
        header = """<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:p><w:r><w:drawing/></w:r><w:r><w:pict/></w:r></w:p>
        </w:hdr>"""
        footer = """<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:p><w:r><w:drawing/></w:r></w:p>
        </w:ftr>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", CONTENT_TYPES)
            archive.writestr("word/document.xml", document)
            archive.writestr("word/header1.xml", header)
            archive.writestr("word/footer2.xml", footer)

        semantic = inspect_file(path)["inspection"]["semantic"]

        self.assertEqual(semantic["drawing_count"], 4)
        self.assertEqual(semantic["body_drawing_count"], 1)
        self.assertEqual(semantic["header_drawing_count"], 2)
        self.assertEqual(semantic["footer_drawing_count"], 1)
        self.assertEqual(semantic["header_parts"], ["word/header1.xml"])
        self.assertEqual(semantic["footer_parts"], ["word/footer2.xml"])

    def test_package_comparison_reports_changed_part_and_macro_preservation(
        self,
    ) -> None:
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
        self.assertEqual(
            result["schema"], "ai.neoharness.office.document-comparison.v3"
        )
        self.assertEqual(result["spreadsheet"]["formulas"]["count"], 0)

    def test_package_comparison_reports_formula_and_sensitive_structure_changes(
        self,
    ) -> None:
        before = self.root / "before.xlsx"
        after = self.root / "after.xlsx"
        self._xlsx(
            before,
            extra_parts={
                "xl/media/image1.png": b"before-image",
                "xl/printerSettings/printerSettings1.bin": b"printer",
            },
        )
        self._xlsx(
            after,
            formula="B1*3",
            sheet_state="hidden",
            defined_name_value="'Free form'!$A$1:$G$9",
            extra_parts={"xl/media/image1.png": b"after-image"},
        )

        result = compare_files(before, after)
        spreadsheet = result["spreadsheet"]
        self.assertEqual(spreadsheet["formulas"]["count"], 1)
        self.assertEqual(spreadsheet["formulas"]["changes"][0]["cell"], "A2")
        self.assertEqual(spreadsheet["sheet_states"]["count"], 1)
        self.assertEqual(spreadsheet["defined_names"]["count"], 1)
        sensitive = result["package"]["sensitive_part_changes"]
        self.assertIn(
            ("xl/media/image1.png", "changed"),
            {(item["part"], item["change"]) for item in sensitive},
        )
        self.assertIn(
            ("xl/printerSettings/printerSettings1.bin", "removed"),
            {(item["part"], item["change"]) for item in sensitive},
        )

    def test_boundary_errors_name_the_offending_path(self) -> None:
        # A relative path with three path-bearing arguments used to produce
        # "file path must be absolute beneath /workspace" with no clue which
        # argument was wrong; the offending value is now part of the message.
        with self.assertRaisesRegex(
            Exception, r"must be absolute beneath /workspace: got 'plan\.json'"
        ):
            exact_read_file("plan.json")

    def test_formula_translation_shifts_only_unanchored_references(self) -> None:
        self.assertEqual(_translate_formula("A1+$B$2", "C3", "D5"), "B3+$B$2")
        self.assertEqual(_translate_formula("$A1+B$2", "C3", "D5"), "$A3+C$2")
        self.assertEqual(
            _translate_formula("SUM(A1:B2)*2", "C3", "C4"), "SUM(A2:B3)*2"
        )
        # Sheet-qualified relative references shift; quoted names, string
        # literals, structured references, and function names never do.
        self.assertEqual(
            _translate_formula("'My A1 Sheet'!A1+Sheet2!B2", "C3", "D5"),
            "'My A1 Sheet'!B3+Sheet2!C4",
        )
        self.assertEqual(
            _translate_formula('IF(A1="B2",1,0)', "C3", "C4"), 'IF(A2="B2",1,0)'
        )
        self.assertEqual(
            _translate_formula("LOG10(A1)", "C3", "C4"), "LOG10(A2)"
        )
        self.assertEqual(
            _translate_formula("SUM(Table1[Col A1])", "C3", "C4"),
            "SUM(Table1[Col A1])",
        )
        # A translation that leaves the grid reports None instead of guessing.
        self.assertIsNone(_translate_formula("A1", "B2", "A1"))

    def test_shared_formula_reindexing_is_normalization_not_change(self) -> None:
        before = self.root / "shared-before.xlsx"
        after = self.root / "shared-after.xlsx"
        # Master B2 with followers B3/B4 sharing one index.
        self._shared_formula_xlsx(
            before,
            '<row r="2"><c r="B2"><f t="shared" ref="B2:B4" si="0">A2*2</f><v>2</v></c></row>'
            '<row r="3"><c r="B3"><f t="shared" si="0"/><v>4</v></c></row>'
            '<row r="4"><c r="B4"><f t="shared" si="0"/><v>6</v></c></row>',
        )
        # Same effective formulas after a round trip: B2 became explicit, the
        # master moved to B3 under a new index and span.
        self._shared_formula_xlsx(
            after,
            '<row r="2"><c r="B2"><f>A2*2</f><v>2</v></c></row>'
            '<row r="3"><c r="B3"><f t="shared" ref="B3:B4" si="7">A3*2</f><v>4</v></c></row>'
            '<row r="4"><c r="B4"><f t="shared" si="7"/><v>6</v></c></row>',
        )
        spreadsheet = compare_files(before, after)["spreadsheet"]
        self.assertEqual(spreadsheet["formulas"]["count"], 0)
        normalizations = spreadsheet["formula_normalizations"]
        self.assertEqual(normalizations["count"], 3)
        by_cell = {item["cell"]: item for item in normalizations["changes"]}
        self.assertEqual(by_cell["B2"]["before_storage"], "shared_master")
        self.assertEqual(by_cell["B2"]["after_storage"], "explicit")
        self.assertEqual(by_cell["B3"]["before_storage"], "shared_follower")
        self.assertEqual(by_cell["B3"]["after_storage"], "shared_master")
        self.assertEqual(by_cell["B4"]["shared_index_before"], "0")
        self.assertEqual(by_cell["B4"]["shared_index_after"], "7")
        self.assertEqual(by_cell["B4"]["effective"], "A4*2")

    def test_true_formula_change_stays_high_salience_amid_reshuffling(self) -> None:
        before = self.root / "true-before.xlsx"
        after = self.root / "true-after.xlsx"
        self._shared_formula_xlsx(
            before,
            '<row r="2"><c r="B2"><f t="shared" ref="B2:B3" si="0">A2*2</f><v>2</v></c></row>'
            '<row r="3"><c r="B3"><f t="shared" si="0"/><v>4</v></c></row>',
        )
        # B2 keeps its effective formula through a reshuffle; B3 genuinely
        # changes from A3*2 to A3*9.
        self._shared_formula_xlsx(
            after,
            '<row r="2"><c r="B2"><f>A2*2</f><v>2</v></c></row>'
            '<row r="3"><c r="B3"><f>A3*9</f><v>36</v></c></row>',
        )
        spreadsheet = compare_files(before, after)["spreadsheet"]
        self.assertEqual(spreadsheet["formula_normalizations"]["count"], 1)
        self.assertEqual(spreadsheet["formulas"]["count"], 1)
        change = spreadsheet["formulas"]["changes"][0]
        self.assertEqual(change["cell"], "B3")
        self.assertEqual(change["before"]["effective"], "A3*2")
        self.assertEqual(change["after"]["effective"], "A3*9")

    def test_orphaned_shared_follower_falls_back_to_visible_change(self) -> None:
        before = self.root / "orphan-before.xlsx"
        after = self.root / "orphan-after.xlsx"
        self._shared_formula_xlsx(
            before,
            '<row r="2"><c r="B2"><f>A2*2</f><v>2</v></c></row>',
        )
        # A follower whose master is missing cannot resolve an effective
        # formula; the comparison must surface it rather than hide it.
        self._shared_formula_xlsx(
            after,
            '<row r="2"><c r="B2"><f t="shared" si="3"/><v>2</v></c></row>',
        )
        spreadsheet = compare_files(before, after)["spreadsheet"]
        self.assertEqual(spreadsheet["formulas"]["count"], 1)
        self.assertEqual(spreadsheet["formula_normalizations"]["count"], 0)

    def test_surgical_docx_operations_preserve_macro_and_target_exact_state(
        self,
    ) -> None:
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
        self.assertEqual(result["provenance"]["finalizer"]["class"], "surgical_ooxml")
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
            self.assertIn(b'fldCharType="begin"', document)
            self.assertIn(b"DATE", document)
            self.assertIn(b"updateFields", settings)
            self.assertEqual(archive.read("word/vbaProject.bin"), b"macro-bytes")

    def test_surgical_xlsx_updates_disconnected_cells_without_flattening_package(
        self,
    ) -> None:
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
            self.assertIn(b'fullCalcOnLoad="1"', workbook)
            self.assertEqual(archive.read("xl/vbaProject.bin"), b"macro-bytes")

    def test_surgical_xlsx_value_change_recalculates_and_rejects_ambiguous_formula_text(
        self,
    ) -> None:
        source = self.root / "source.xlsx"
        value_output = self.root / "value-output.xlsx"
        ambiguous_output = self.root / "ambiguous-output.xlsx"
        literal_output = self.root / "literal-output.xlsx"
        self._xlsx(source)

        result = update_xlsx_cells(
            source,
            value_output,
            updates={"Free form": {"B1": 7}},
        )
        self.assertIs(result["details"]["formula_recalculation_requested"], True)
        with zipfile.ZipFile(value_output) as archive:
            workbook = archive.read("xl/workbook.xml")
            self.assertIn(b'fullCalcOnLoad="1"', workbook)
            self.assertIn(b'forceFullCalc="1"', workbook)

        with self.assertRaisesRegex(OoxmlMutationError, "scalar value beginning"):
            update_xlsx_cells(
                source,
                ambiguous_output,
                updates={"Free form": {"B1": "=A1+1"}},
            )
        self.assertFalse(ambiguous_output.exists())

        update_xlsx_cells(
            source,
            literal_output,
            updates={"Free form": {"B1": {"value": "=literal text"}}},
        )
        with zipfile.ZipFile(literal_output) as archive:
            self.assertIn(
                b"=literal text",
                archive.read("xl/worksheets/sheet1.xml"),
            )

    def test_surgical_xlsx_formatting_preserves_values_formulas_and_package(
        self,
    ) -> None:
        source = self.root / "source.xlsx"
        output = self.root / "output.xlsx"
        self._xlsx(
            source,
            extra_parts={"xl/printerSettings/printerSettings1.bin": b"printer"},
        )

        result = format_xlsx(
            source,
            output,
            plan={
                "Free form": {
                    "ranges": [
                        {
                            "range": "A1:B1",
                            "font": {"bold": True, "color": "FFFFFF", "size": 14},
                            "fill": "1F4E78",
                            "alignment": {
                                "horizontal": "center",
                                "vertical": "center",
                                "wrap_text": True,
                            },
                            "border": {"style": "thin", "color": "D9E2F3"},
                            "apply_to_blank_cells": True,
                        }
                    ],
                    "columns": {"A": {"width": 18}, "B": {"width": 14}},
                    "rows": {"1": {"height": 24}},
                    "freeze_panes": "A2",
                    "show_gridlines": False,
                    "tab_color": "1F4E78",
                    "state": "hidden",
                }
            },
        )

        self.assertEqual(result["details"]["styled_cell_count"], 2)
        with zipfile.ZipFile(source) as before, zipfile.ZipFile(output) as after:
            before_sheet = ET.fromstring(before.read("xl/worksheets/sheet1.xml"))
            after_sheet = ET.fromstring(after.read("xl/worksheets/sheet1.xml"))
            before_formula = before_sheet.find(
                ".//s:c[@r='A2']/s:f",
                {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"},
            )
            after_formula = after_sheet.find(
                ".//s:c[@r='A2']/s:f",
                {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"},
            )
            self.assertEqual(before_formula.text, after_formula.text)
            self.assertEqual(
                before.read("xl/printerSettings/printerSettings1.bin"),
                after.read("xl/printerSettings/printerSettings1.bin"),
            )
            serialized_sheet = after.read("xl/worksheets/sheet1.xml")
            self.assertIn(b'mc:Ignorable="x14ac xr2"', serialized_sheet)
            self.assertIn(b'xmlns:x14ac="http://schemas.microsoft.com/', serialized_sheet)
            self.assertIn(b'xmlns:xr2="http://schemas.microsoft.com/', serialized_sheet)
        comparison = result["comparison"]
        self.assertEqual(comparison["spreadsheet"]["formulas"]["count"], 0)
        self.assertEqual(comparison["spreadsheet"]["sheet_states"]["count"], 1)
        self.assertEqual(comparison["package"]["removed_parts"], [])

    @unittest.skipUnless(
        shutil.which("vips") and shutil.which("convert"), "image tools"
    )
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
        self.assertEqual(rendered["provenance"]["finalizer"]["class"], "pdf_render")
        self.assertEqual(ocr_result["provenance"]["finalizer"]["class"], "pdf_ocr")

    def test_quality_policy_rejects_unapproved_or_mismatched_evidence(self) -> None:
        artifact = self.root / "candidate.docx"
        artifact.write_bytes(b"candidate")
        provenance = artifact_provenance(
            artifact,
            finalizer="unqualified",
            producer="libreoffice",
            operation="save",
        )
        with self.assertRaisesRegex(QualityPolicyError, "cannot qualify revise/ooxml"):
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
