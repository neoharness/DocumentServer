from __future__ import annotations

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
        self.output.mkdir()
        self.store = attestation.AttestationStore(self.root / "attestations")
        self.output_patch = patch.object(attestation, "OUTPUT_ROOT", self.output)
        self.output_patch.start()

    def tearDown(self) -> None:
        self.output_patch.stop()
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


if __name__ == "__main__":
    unittest.main()
