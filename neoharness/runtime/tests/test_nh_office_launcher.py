"""Launcher contract tests for nh-office.

Covers the native-create example topics, the literal jsValue host-bridge
comment, the narrow bridge-misuse preflight, deterministic failure
classification, and the executable truth of the printed --help commands.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import re
import shlex
import sys
import unittest

SOURCE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE_ROOT / "python"))

from neoharness_office import cli as document_cli  # noqa: E402


def _load_launcher():
    candidates = (
        SOURCE_ROOT / "bin" / "nh-office",
        Path("/opt/neoharness-office/bin/nh-office"),
    )
    launcher_path = next(path for path in candidates if path.is_file())
    loader = importlib.machinery.SourceFileLoader(
        "nh_office_launcher", str(launcher_path)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


LAUNCHER = _load_launcher()


class ExampleTopicTests(unittest.TestCase):
    def test_every_format_has_revise_and_create_topics(self) -> None:
        expected = {
            f"{family}-{operation}"
            for family in ("docx", "xlsx", "pptx", "pdf")
            for operation in ("revise", "create")
        }
        expected.add("docx-to-pdf")
        self.assertEqual(set(LAUNCHER.EXAMPLES), expected)

    def test_every_example_teaches_the_literal_bridge(self) -> None:
        for topic, script in LAUNCHER.EXAMPLES.items():
            self.assertIn('"jsValue(', script, topic)
            self.assertIn("LITERAL Document", script, topic)
            self.assertIn("Argument.input or Argument.output", script, topic)

    def test_every_example_passes_its_own_preflight(self) -> None:
        for topic, script in LAUNCHER.EXAMPLES.items():
            self.assertEqual(
                LAUNCHER.bridge_misuse_findings(script), [], topic
            )

    def test_create_topics_create_natively(self) -> None:
        self.assertIn('builder.CreateFile("docx")', LAUNCHER.EXAMPLES["docx-create"])
        self.assertIn('builder.CreateFile("xlsx")', LAUNCHER.EXAMPLES["xlsx-create"])
        self.assertIn('builder.CreateFile("pptx")', LAUNCHER.EXAMPLES["pptx-create"])
        # PDF creation authors through the Word surface then saves as pdf.
        self.assertIn('builder.CreateFile("docx")', LAUNCHER.EXAMPLES["pdf-create"])
        self.assertIn('builder.SaveFile("pdf"', LAUNCHER.EXAMPLES["pdf-create"])
        for topic in ("docx-create", "xlsx-create", "pptx-create", "pdf-create"):
            self.assertNotIn("OpenFile", LAUNCHER.EXAMPLES[topic], topic)

    def test_docx_to_pdf_opens_source_and_saves_directly(self) -> None:
        script = LAUNCHER.EXAMPLES["docx-to-pdf"]
        self.assertNotIn("CreateFile", script)
        open_source = script.index('builder.OpenFile("jsValue(inputPath)")')
        save_pdf = script.index('builder.SaveFile("pdf"')
        self.assertLess(open_source, save_pdf)
        self.assertIn('Argument["output"]', script)
        self.assertNotIn('Argument["outputs"]', script)
        self.assertNotIn('builder.SaveFile("docx"', script)
        self.assertIn("never overwrite the input", script)

    def test_create_image_reference_inserts_returned_drawing(self) -> None:
        candidates = (
            SOURCE_ROOT / "share" / "api-reference" / "builder-host.js",
            Path(
                "/opt/neoharness-office/share/api-reference/builder-host.js"
            ),
        )
        reference_path = next(path for path in candidates if path.is_file())
        reference = reference_path.read_text(encoding="utf-8")
        self.assertIn("var image = Api.CreateImage", reference)
        self.assertIn("AddDrawing(image)", reference)
        self.assertIn("does not insert the drawing by itself", reference)
        self.assertIn("Existing images in a DOCX", reference)
        self.assertIn("only for this fallback", reference)


class BridgePreflightTests(unittest.TestCase):
    def test_detects_the_exact_incident_misuse(self) -> None:
        script = (
            'builder.CreateFile("docx");\n'
            "var oDocument = Api.GetDocument();\n"
            'oDocument.GetElement(0).AddText("probe");\n'
            'builder.SaveFile("docx", Argument.output);\n'
            "builder.CloseFile();\n"
        )
        findings = LAUNCHER.bridge_misuse_findings(script)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["line"], 4)
        self.assertEqual(findings[0]["method"], "SaveFile")
        self.assertEqual(findings[0]["token"], "Argument.output")

    def test_detects_bare_input_in_openfile(self) -> None:
        findings = LAUNCHER.bridge_misuse_findings("builder.OpenFile(Argument.input);")
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["token"], "Argument.input")

    def test_indented_builder_lines_are_still_native_commands(self) -> None:
        # The engine treats a line whose TRIMMED start is builder.* as native,
        # so an indented call inside a block carries the same misuse.
        findings = LAUNCHER.bridge_misuse_findings(
            'if (x) {\n    builder.SaveFile("docx", Argument.output);\n}\n'
        )
        self.assertEqual(len(findings), 1)

    def test_correct_bridge_and_static_literals_pass(self) -> None:
        for script in (
            'builder.OpenFile("jsValue(inputPath)");',
            'builder.SaveFile("docx", "jsValue(outputPath)");',
            'builder.SaveFile("docx", "/workspace/output/report.docx");',
        ):
            self.assertEqual(LAUNCHER.bridge_misuse_findings(script), [], script)

    def test_pure_javascript_and_comments_are_out_of_scope(self) -> None:
        for script in (
            'var outputPath = Argument.output;\n',
            'var x = Argument["output"];\n',
            '// builder.SaveFile("docx", Argument.output);\n',
        ):
            self.assertEqual(LAUNCHER.bridge_misuse_findings(script), [], script)

    def test_bracket_form_is_intentionally_out_of_the_narrow_scope(self) -> None:
        # Only the proven bare dot-form misuse is preflighted; the bracket
        # form fails differently in the engine and is not rewritten here.
        findings = LAUNCHER.bridge_misuse_findings(
            'builder.SaveFile("docx", Argument["output"]);'
        )
        self.assertEqual(findings, [])

    def test_message_carries_exact_corrective_syntax(self) -> None:
        findings = LAUNCHER.bridge_misuse_findings(
            'builder.SaveFile("docx", Argument.output);'
        )
        message = LAUNCHER.bridge_misuse_message(findings)
        self.assertIn("bridge_contract:", message)
        self.assertIn('var outputPath = Argument["output"];', message)
        self.assertIn('"jsValue(outputPath)"', message)
        self.assertIn('builder.OpenFile("jsValue(inputPath)");', message)


class FailureClassificationTests(unittest.TestCase):
    def _classify(self, **overrides):
        arguments = {
            "status": "failed",
            "engine_returncode": 70,
            "stdout_text": "",
            "stderr_text": "",
            "expected_outputs": [Path("/workspace/output/a.docx")],
            "artifacts": [],
            "artifact_errors": [],
        }
        arguments.update(overrides)
        return LAUNCHER._classify_failure(**arguments)

    def test_success_has_no_failure_fact(self) -> None:
        self.assertIsNone(self._classify(status="succeeded", engine_returncode=0))

    def test_timeout_and_signal_classes(self) -> None:
        self.assertEqual(self._classify(status="timed_out")["class"], "timeout")
        terminated = self._classify(status="terminated", engine_returncode=-9)
        self.assertEqual(terminated["class"], "terminated_by_signal")
        self.assertEqual(terminated["signal"], 9)

    def test_silent_failure_names_the_reproduced_cause(self) -> None:
        failure = self._classify()
        self.assertEqual(failure["class"], "engine_silent_failure")
        self.assertIn("jsValue(variableName)", failure["hint"])

    def test_fragment_syntax_error_hint(self) -> None:
        failure = self._classify(
            stderr_text=(
                "SyntaxError: Unexpected end of input\n"
                "SyntaxError: Unexpected end of input\n"
            )
        )
        self.assertEqual(failure["class"], "js_exception")
        self.assertIn("SyntaxError", failure["evidence"])
        self.assertIn("top level", failure["hint"])

    def test_builder_prebinding_hint(self) -> None:
        failure = self._classify(
            stderr_text="ReferenceError: builder is not defined at <anonymous>:1:56"
        )
        self.assertEqual(failure["class"], "js_exception")
        self.assertIn("not a JavaScript global", failure["hint"])

    def test_empty_save_path_signature_hint(self) -> None:
        failure = self._classify(
            stdout_text="Empty sFileFrom or sFileTo\nerror: : save file error (88)"
        )
        self.assertEqual(failure["class"], "engine_error")
        self.assertIn('builder.SaveFile("docx", "jsValue(p)")', failure["hint"])

    def test_generic_engine_error_keeps_evidence_without_invention(self) -> None:
        failure = self._classify(stderr_text="engine exploded for reasons")
        self.assertEqual(failure["class"], "engine_error")
        self.assertNotIn("hint", failure)
        self.assertIn("engine exploded", failure["evidence"])

    def test_missing_artifact_class(self) -> None:
        failure = self._classify(engine_returncode=0)
        self.assertEqual(failure["class"], "missing_artifact")
        self.assertEqual(failure["missing"], ["/workspace/output/a.docx"])


class HelpEpilogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.help_text = LAUNCHER._parser().format_help()

    def test_start_here_commands_use_real_flags(self) -> None:
        self.assertIn("--output /workspace/work/inspection.json", self.help_text)
        self.assertNotIn("--json-output", self.help_text)

    def test_builder_host_reference_is_listed(self) -> None:
        self.assertIn(
            "/opt/neoharness-office/share/api-reference/builder-host.js",
            self.help_text,
        )
        self.assertIn('"jsValue(variableName)"', self.help_text)

    def test_printed_inspect_command_parses_against_nh_document(self) -> None:
        # Execute the printed command shape at the argparse layer: the exact
        # argv from the epilog must be accepted by nh-document's real parser.
        lines = [
            line.strip()
            for line in self.help_text.splitlines()
            if line.strip().startswith("nh-document inspect")
        ]
        self.assertEqual(len(lines), 1)
        argv = shlex.split(lines[0])[1:]
        namespace = document_cli._parser().parse_args(argv)
        self.assertEqual(namespace.command, "inspect")
        self.assertEqual(namespace.files, ["/workspace/input/source.xlsx"])
        self.assertEqual(namespace.output, "/workspace/work/inspection.json")

    def test_printed_run_command_parses_against_nh_office(self) -> None:
        merged = re.sub(r"\\\n\s*", " ", self.help_text)
        lines = [
            line.strip()
            for line in merged.splitlines()
            if line.strip().startswith("nh-office run")
        ]
        self.assertEqual(len(lines), 1)
        argv = shlex.split(lines[0])[1:]
        namespace = LAUNCHER._parser().parse_args(argv)
        self.assertEqual(namespace.command, "run")
        self.assertEqual(namespace.script, "/workspace/work/edit.js")
        self.assertEqual(namespace.output, ["/workspace/output/candidate.xlsx"])

if __name__ == "__main__":
    unittest.main()
