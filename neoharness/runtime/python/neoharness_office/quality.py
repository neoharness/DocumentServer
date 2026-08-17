from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .paths import file_fact


POLICY_SCHEMA = "ai.neoharness.office.quality-policy.v1"
PROVENANCE_SCHEMA = "ai.neoharness.office.artifact-provenance.v1"
QUALIFICATION_SCHEMA = "ai.neoharness.office.artifact-qualification.v1"

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
    ".vsdx",
    ".vssx",
    ".vstx",
    ".vstm",
    ".vssm",
    ".vsdm",
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
}


class QualityPolicyError(ValueError):
    """An artifact lacks exact evidence required by the quality policy."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _policy_candidates() -> tuple[Path, ...]:
    explicit = os.environ.get("NHO_QUALITY_POLICY_PATH")
    source_tree = Path(__file__).resolve().parents[2] / "quality-policy.v1.json"
    installed = (
        Path(os.environ.get("NHO_INSTALL_ROOT", "/opt/neoharness-office"))
        / "share"
        / "runtime"
        / "quality-policy.v1.json"
    )
    return (Path(explicit),) if explicit else (installed, source_tree)


def load_policy(path: Path | None = None) -> tuple[dict[str, Any], dict[str, str]]:
    candidates = (path,) if path is not None else _policy_candidates()
    selected = next((candidate for candidate in candidates if candidate.is_file()), None)
    if selected is None:
        searched = ", ".join(str(candidate) for candidate in candidates)
        raise QualityPolicyError(f"quality policy is missing; searched: {searched}")
    raw = selected.read_bytes()
    try:
        policy = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualityPolicyError(f"quality policy is invalid: {selected}: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schema") != POLICY_SCHEMA:
        raise QualityPolicyError(f"quality policy has an unsupported schema: {selected}")
    version = policy.get("version")
    if not isinstance(version, str) or not version:
        raise QualityPolicyError(f"quality policy version is missing: {selected}")
    return policy, {
        "version": version,
        "sha256": _sha256_bytes(raw),
    }


def policy_identity(path: Path | None = None) -> dict[str, str]:
    _, identity = load_policy(path)
    return identity


def artifact_family(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in OOXML_EXTENSIONS:
        return "ooxml"
    if extension == ".pdf":
        return "pdf"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    raise QualityPolicyError(
        f"quality policy does not define an artifact family for {path.name}"
    )


def artifact_provenance(
    path: Path,
    *,
    finalizer: str,
    producer: str,
    operation: str,
    policy_path: Path | None = None,
) -> dict[str, object]:
    policy, identity = load_policy(policy_path)
    finalizers = policy.get("finalizers")
    if not isinstance(finalizers, dict) or finalizer not in finalizers:
        raise QualityPolicyError(f"unknown quality-policy finalizer: {finalizer}")
    if not producer or not operation:
        raise QualityPolicyError("artifact producer and operation must be explicit")
    return {
        "schema": PROVENANCE_SCHEMA,
        "policy": identity,
        "finalizer": {
            "class": finalizer,
            "producer": producer,
            "operation": operation,
        },
        "artifact": file_fact(path),
    }


def _mapping(value: object, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualityPolicyError(f"{label} must be an object")
    return value


def qualify_artifact(
    path: Path,
    *,
    intent: str,
    provenance: Mapping[str, Any],
    family: str | None = None,
    policy_path: Path | None = None,
) -> dict[str, object]:
    policy, identity = load_policy(policy_path)
    if provenance.get("schema") != PROVENANCE_SCHEMA:
        raise QualityPolicyError("artifact provenance schema is missing or unsupported")

    attested_policy = _mapping(provenance.get("policy"), label="provenance policy")
    if dict(attested_policy) != identity:
        raise QualityPolicyError("artifact provenance uses a stale or mismatched policy")

    finalizer = _mapping(provenance.get("finalizer"), label="provenance finalizer")
    finalizer_class = finalizer.get("class")
    if not isinstance(finalizer_class, str):
        raise QualityPolicyError("artifact finalizer class is missing")

    actual_family = family or artifact_family(path)
    rules = _mapping(policy.get("rules"), label="quality-policy rules")
    intent_rules = _mapping(rules.get(intent), label=f"quality-policy intent {intent}")
    allowed = intent_rules.get(actual_family)
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise QualityPolicyError(
            f"quality policy has no publication rule for {intent}/{actual_family}"
        )
    if finalizer_class not in allowed:
        raise QualityPolicyError(
            f"finalizer {finalizer_class} cannot qualify {intent}/{actual_family} bytes"
        )

    attested_artifact = _mapping(
        provenance.get("artifact"), label="provenance artifact"
    )
    actual = file_fact(path)
    for coordinate in ("size", "sha256"):
        if attested_artifact.get(coordinate) != actual[coordinate]:
            raise QualityPolicyError(
                f"artifact {coordinate} does not match finalizer provenance"
            )

    return {
        "schema": QUALIFICATION_SCHEMA,
        "status": "qualified",
        "intent": intent,
        "family": actual_family,
        "policy": identity,
        "finalizer": dict(finalizer),
        "artifact": actual,
    }


def find_artifact_provenance(value: object, path: Path) -> Mapping[str, Any]:
    """Find exact provenance in a server-observed operation result.

    This convenience parser does not make an arbitrary JSON file trustworthy.
    A publication boundary must bind the result to the tool execution it
    observed and must not accept a model-supplied evidence file.
    """

    expected = file_fact(path)

    def visit(candidate: object) -> Mapping[str, Any] | None:
        if isinstance(candidate, Mapping):
            if candidate.get("schema") == PROVENANCE_SCHEMA:
                artifact = candidate.get("artifact")
                if (
                    isinstance(artifact, Mapping)
                    and artifact.get("sha256") == expected["sha256"]
                    and artifact.get("size") == expected["size"]
                ):
                    return candidate
            for key in ("provenance", "result", "artifacts"):
                found = visit(candidate.get(key))
                if found is not None:
                    return found
        elif isinstance(candidate, list):
            for item in candidate:
                found = visit(item)
                if found is not None:
                    return found
        return None

    result = visit(value)
    if result is None:
        raise QualityPolicyError("no artifact-bound finalizer provenance was found")
    return result
