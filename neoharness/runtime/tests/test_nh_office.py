from __future__ import annotations

import argparse
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


LAUNCHER = Path(__file__).resolve().parents[1] / "bin" / "nh-office"
LOADER = importlib.machinery.SourceFileLoader("nh_office_launcher", str(LAUNCHER))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
nh_office = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(nh_office)


class LauncherContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.input = self.workspace / "input"
        self.work = self.workspace / "work"
        self.output = self.workspace / "output"
        for path in (self.input, self.work, self.output):
            path.mkdir(parents=True)

        self.patchers = [
            mock.patch.object(nh_office, "WORKSPACE_ROOT", self.workspace),
            mock.patch.object(nh_office, "INPUT_ROOT", self.input),
            mock.patch.object(nh_office, "WORK_ROOT", self.work),
            mock.patch.object(nh_office, "OUTPUT_ROOT", self.output),
            mock.patch.object(
                nh_office,
                "ARTIFACT_READ_ROOTS",
                (self.input, self.work, self.output),
            ),
            mock.patch.object(
                nh_office,
                "DEFAULT_MANIFEST",
                self.output / "nh-office-manifest.json",
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def _write_quality_policy(self, install_root: Path) -> None:
        policy = install_root / "share/runtime/quality-policy.v1.json"
        policy.parent.mkdir(parents=True)
        policy.write_text(
            json.dumps(
                {
                    "schema": "ai.neoharness.office.quality-policy.v1",
                    "version": "neoharness-office-quality.v1",
                    "finalizers": {"native_office": {}},
                    "rules": {},
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_input_requires_regular_no_follow_file_inside_exact_root(self) -> None:
        source = self.input / "source.docx"
        source.write_bytes(b"source")
        self.assertEqual(
            nh_office._exact_input(str(source), self.input, label="input"),
            source,
        )

        symlink = self.input / "linked.docx"
        symlink.symlink_to(source)
        with self.assertRaisesRegex(nh_office.ContractError, "not a symlink"):
            nh_office._exact_input(str(symlink), self.input, label="input")

        outside = self.root / "outside.docx"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(nh_office.ContractError, "must remain beneath"):
            nh_office._exact_input(str(outside), self.input, label="input")

    def test_help_and_examples_are_useful_without_a_workspace_manifest(self) -> None:
        help_result = subprocess.run(
            [sys.executable, str(LAUNCHER), "--help"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn("nh-office examples xlsx-revise", help_result.stdout)
        self.assertIn("cell-apiBuilder.js", help_result.stdout)

        example_result = subprocess.run(
            [sys.executable, str(LAUNCHER), "examples", "xlsx-revise"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.assertIn('builder.OpenFile("jsValue(inputPath)")', example_result.stdout)
        self.assertIn("SetFillColor", example_result.stdout)

    def test_output_rejects_escape_and_symlink(self) -> None:
        outside = self.root / "outside.docx"
        with self.assertRaisesRegex(nh_office.ContractError, "must remain beneath"):
            nh_office._exact_output(str(outside), label="output")

        target = self.output / "target.docx"
        target.write_bytes(b"target")
        linked = self.output / "linked.docx"
        linked.symlink_to(target)
        with self.assertRaisesRegex(nh_office.ContractError, "regular file"):
            nh_office._exact_output(str(linked), label="output")

        with self.assertRaisesRegex(nh_office.ContractError, "fresh path"):
            nh_office._exact_output(
                str(target), label="output", require_absent=True
            )

    def test_intermediate_and_produced_artifacts_can_be_reopened(self) -> None:
        for root in (self.input, self.work, self.output):
            source = root / "source.docx"
            source.write_bytes(b"source")
            self.assertEqual(
                nh_office._exact_artifact_input(str(source), label="input"),
                source,
            )

        outside = self.root / "outside.docx"
        outside.write_bytes(b"outside")
        with self.assertRaisesRegex(nh_office.ContractError, "must remain beneath"):
            nh_office._exact_artifact_input(str(outside), label="input")

    def test_diagnostic_capture_is_bounded_but_counts_exact_bytes(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import os; os.write(1, b'o' * 4096); os.write(2, b'e' * 3072)",
        ]
        result = nh_office._run_bounded(
            command,
            cwd=self.work,
            env=dict(os.environ),
            timeout_seconds=5,
            diagnostic_limit=128,
        )
        returncode, stdout, stderr, stdout_bytes, stderr_bytes, timed_out = result
        self.assertEqual(returncode, 0)
        self.assertFalse(timed_out)
        self.assertEqual(len(stdout), 128)
        self.assertEqual(len(stderr), 128)
        self.assertEqual(stdout_bytes, 4096)
        self.assertEqual(stderr_bytes, 3072)

    def test_timeout_terminates_the_process_group(self) -> None:
        started = time.monotonic()
        result = nh_office._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=self.work,
            env=dict(os.environ),
            timeout_seconds=0.05,
            diagnostic_limit=128,
        )
        self.assertEqual(result[0], nh_office.EXIT_TIMEOUT)
        self.assertTrue(result[-1])
        self.assertLess(time.monotonic() - started, 3)

    def test_real_launcher_contract_uses_deterministic_font_cache(self) -> None:
        install_root = self.root / "install"
        binary_root = install_root / "documentserver/server/FileConverter/bin"
        binary_root.mkdir(parents=True)
        (install_root / "VERSION").write_text("9.3.3-nh1\n", encoding="utf-8")
        self._write_quality_policy(install_root)
        (binary_root / "AllFonts.js").write_text("font-cache\n", encoding="utf-8")
        engine = binary_root / "docbuilder"
        engine.write_text(
            "#!/usr/bin/python3\n"
            "import json, pathlib, sys\n"
            "assert '--check-fonts=0' in sys.argv\n"
            "assert '--fonts-system=false' in sys.argv\n"
            "raw = next(v for v in sys.argv if v.startswith('--argument='))\n"
            "argument = json.loads(raw.split('=', 1)[1])\n"
            "pathlib.Path(argument['output']).write_bytes(b'produced')\n"
            "print('engine-ok')\n",
            encoding="utf-8",
        )
        engine.chmod(0o755)
        script = self.work / "edit.js"
        script.write_text("// fake engine owns this test\n", encoding="utf-8")
        output = self.output / "candidate.docx"
        manifest = self.output / "result.json"
        args = argparse.Namespace(
            script=str(script),
            input=[],
            output=[str(output)],
            argument=None,
            manifest=str(manifest),
            timeout=5,
            max_diagnostic_bytes=1024,
        )

        with mock.patch.dict(os.environ, {"NHO_INSTALL_ROOT": str(install_root)}):
            self.assertEqual(nh_office._run(args), 0)

        result = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["runtime_version"], "9.3.3-nh1")
        self.assertEqual(result["artifacts"][0]["size"], len(b"produced"))
        provenance = result["artifacts"][0]["provenance"]
        self.assertEqual(provenance["finalizer"]["class"], "native_office")
        self.assertEqual(
            provenance["artifact"]["sha256"], result["artifacts"][0]["sha256"]
        )
        self.assertEqual(
            provenance["policy"]["sha256"], result["quality_policy"]["sha256"]
        )
        self.assertEqual(result["diagnostics"]["stdout"], "engine-ok\n")

    def test_partial_multi_file_result_retains_successful_sibling(self) -> None:
        install_root = self.root / "install"
        binary_root = install_root / "documentserver/server/FileConverter/bin"
        binary_root.mkdir(parents=True)
        (install_root / "VERSION").write_text("9.3.3-nh1\n", encoding="utf-8")
        self._write_quality_policy(install_root)
        (binary_root / "AllFonts.js").write_text("font-cache\n", encoding="utf-8")
        engine = binary_root / "docbuilder"
        engine.write_text(
            "#!/usr/bin/python3\n"
            "import json, pathlib, sys\n"
            "raw = next(v for v in sys.argv if v.startswith('--argument='))\n"
            "argument = json.loads(raw.split('=', 1)[1])\n"
            "assert len(argument['inputs']) == 2\n"
            "assert len(argument['outputs']) == 2\n"
            "pathlib.Path(argument['outputs'][0]).write_bytes(b'first-succeeded')\n",
            encoding="utf-8",
        )
        engine.chmod(0o755)
        script = self.work / "multi.js"
        script.write_text("// fake engine owns this test\n", encoding="utf-8")
        inputs = [self.input / "one.docx", self.input / "two.xlsx"]
        for item in inputs:
            item.write_bytes(b"source")
        outputs = [self.output / "one.docx", self.output / "two.xlsx"]
        manifest = self.output / "partial.json"
        args = argparse.Namespace(
            script=str(script),
            input=[str(item) for item in inputs],
            output=[str(item) for item in outputs],
            argument=None,
            manifest=str(manifest),
            timeout=5,
            max_diagnostic_bytes=1024,
        )

        with mock.patch.dict(os.environ, {"NHO_INSTALL_ROOT": str(install_root)}):
            self.assertEqual(nh_office._run(args), nh_office.EXIT_ENGINE)

        result = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["expected_outputs"], [str(item) for item in outputs])
        self.assertEqual(len(result["artifacts"]), 1)
        self.assertEqual(result["artifacts"][0]["path"], str(outputs[0]))
        self.assertEqual(result["artifacts"][0]["size"], len(b"first-succeeded"))
        self.assertEqual(
            result["artifacts"][0]["provenance"]["finalizer"]["class"],
            "native_office",
        )

    def test_missing_quality_policy_fails_before_engine_execution(self) -> None:
        install_root = self.root / "install"
        binary_root = install_root / "documentserver/server/FileConverter/bin"
        binary_root.mkdir(parents=True)
        engine_ran = self.root / "engine-ran"
        engine = binary_root / "docbuilder"
        engine.write_text(
            "#!/usr/bin/python3\n"
            f"import pathlib; pathlib.Path({str(engine_ran)!r}).touch()\n",
            encoding="utf-8",
        )
        engine.chmod(0o755)
        script = self.work / "edit.js"
        script.write_text("// should not run\n", encoding="utf-8")
        args = argparse.Namespace(
            script=str(script),
            input=[],
            output=[str(self.output / "candidate.docx")],
            argument=None,
            manifest=str(self.output / "result.json"),
            timeout=5,
            max_diagnostic_bytes=1024,
        )

        with mock.patch.dict(os.environ, {"NHO_INSTALL_ROOT": str(install_root)}):
            self.assertEqual(nh_office._run(args), nh_office.EXIT_DATA)
        self.assertFalse(engine_ran.exists())

    def test_preexisting_output_cannot_acquire_native_finalizer_provenance(self) -> None:
        install_root = self.root / "install"
        binary_root = install_root / "documentserver/server/FileConverter/bin"
        binary_root.mkdir(parents=True)
        self._write_quality_policy(install_root)
        engine_ran = self.root / "engine-ran"
        engine = binary_root / "docbuilder"
        engine.write_text(
            "#!/usr/bin/python3\n"
            f"import pathlib; pathlib.Path({str(engine_ran)!r}).touch()\n",
            encoding="utf-8",
        )
        engine.chmod(0o755)
        script = self.work / "no-op.js"
        script.write_text("// should not run\n", encoding="utf-8")
        output = self.output / "candidate.docx"
        output.write_bytes(b"written-by-an-unqualified-serializer")
        args = argparse.Namespace(
            script=str(script),
            input=[],
            output=[str(output)],
            argument=None,
            manifest=str(self.output / "result.json"),
            timeout=5,
            max_diagnostic_bytes=1024,
        )

        with mock.patch.dict(os.environ, {"NHO_INSTALL_ROOT": str(install_root)}):
            self.assertEqual(nh_office._run(args), nh_office.EXIT_DATA)
        self.assertFalse(engine_ran.exists())
        self.assertEqual(output.read_bytes(), b"written-by-an-unqualified-serializer")


if __name__ == "__main__":
    unittest.main()
