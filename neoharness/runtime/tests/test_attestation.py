from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from neoharness_office import attestation
from neoharness_office.quality import artifact_provenance


class ServerAttestationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "output"
        self.input = self.root / "input"
        self.work = self.root / "work"
        self.output.mkdir()
        self.input.mkdir()
        self.work.mkdir()
        self.store = attestation.AttestationStore(self.root / "attestations")
        self.output_patch = patch.object(attestation, "OUTPUT_ROOT", self.output)
        self.input_patch = patch.object(attestation, "INPUT_ROOT", self.input)
        self.work_patch = patch.object(attestation, "WORK_ROOT", self.work)
        self.output_patch.start()
        self.input_patch.start()
        self.work_patch.start()

    def tearDown(self) -> None:
        self.output_patch.stop()
        self.input_patch.stop()
        self.work_patch.stop()
        self.temporary.cleanup()

    def _record(self, artifact: Path, *, finalizer: str, producer: str) -> None:
        provenance = artifact_provenance(
            artifact,
            finalizer=finalizer,
            producer=producer,
            operation="test",
        )
        self.store.record(
            uid=1000,
            tool="office",
            argv=["run", "/workspace/work/test.js"],
            evidence={"artifacts": [{"provenance": provenance}]},
        )

    def test_exact_server_observation_qualifies_the_same_artifact_hash(self) -> None:
        artifact = self.output / "candidate.docx"
        artifact.write_bytes(b"publication candidate")
        self._record(artifact, finalizer="native_office", producer="docbuilder")

        result = self.store.qualify(artifact=str(artifact), intent="revise")

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["qualification"]["finalizer"]["class"],
            "native_office",
        )
        records = list((self.root / "attestations").glob("*.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(records[0].read_text())["observed_uid"], 1000)

    def test_changed_bytes_and_unapproved_finalizer_are_rejected(self) -> None:
        artifact = self.output / "candidate.docx"
        artifact.write_bytes(b"candidate")
        self._record(artifact, finalizer="native_office", producer="docbuilder")
        artifact.write_bytes(b"changed after finalization")
        with self.assertRaisesRegex(
            attestation.AttestationError,
            "no server-observed approved finalizer",
        ):
            self.store.qualify(artifact=str(artifact), intent="revise")

        artifact.write_bytes(b"unqualified candidate")
        self._record(artifact, finalizer="unqualified", producer="libreoffice")
        with self.assertRaisesRegex(
            attestation.AttestationError,
            "no server-observed approved finalizer",
        ):
            self.store.qualify(artifact=str(artifact), intent="revise")

    def test_gateway_seal_binds_declared_document_input_bytes(self) -> None:
        source = self.input / "source.docx"
        source.write_bytes(b"canonical document bytes")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()

        result = self.store.seal_input(
            path=str(source),
            expected_sha256=digest,
            expected_size=source.stat().st_size,
        )
        lineage = self.store.validate_declared_inputs(
            tool="office",
            argv=["run", "/workspace/work/edit.js", "--input", str(source)],
        )

        self.assertTrue(result["ok"])
        self.assertEqual(lineage[0]["authority"], "gateway_input")
        source.write_bytes(b"mutated after gateway seal")
        with self.assertRaisesRegex(
            attestation.AttestationError,
            "sealed document input bytes changed",
        ):
            self.store.validate_declared_inputs(
                tool="office",
                argv=["run", "/workspace/work/edit.js", f"--input={source}"],
            )

    def test_working_serializer_output_cannot_become_finalizer_input(self) -> None:
        lossy = self.work / "libreoffice-output.docx"
        lossy.write_bytes(b"lossy intermediate")

        with self.assertRaisesRegex(
            attestation.AttestationError,
            "working document bytes cannot become finalizer input",
        ):
            self.store.validate_declared_inputs(
                tool="office",
                argv=["run", "/workspace/work/edit.js", "--input", str(lossy)],
            )

    def test_approved_output_may_feed_a_later_finalizer(self) -> None:
        source = self.output / "first-pass.docx"
        source.write_bytes(b"approved first pass")
        self._record(source, finalizer="native_office", producer="docbuilder")

        lineage = self.store.validate_declared_inputs(
            tool="document",
            argv=[
                "ooxml",
                "replace-text",
                str(source),
                str(self.output / "second-pass.docx"),
            ],
        )

        self.assertEqual(lineage[0]["authority"], "approved_output")

    def test_office_discovery_commands_do_not_require_artifact_evidence(self) -> None:
        for argv in (
            ["--help"],
            ["run", "--help"],
            ["--version"],
            ["examples", "xlsx-revise"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(
                    attestation._execution_evidence("office", argv, b"guidance\n")
                )


if __name__ == "__main__":
    unittest.main()
