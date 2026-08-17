#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'usage: %s RELEASE_ROOT\n' "$0" >&2
  exit 2
fi

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
release_root="$(realpath "$1")"
(
  cd "${release_root}"
  sha256sum --check --strict SHA256SUMS
)

python3 - "${release_root}/RELEASE-MANIFEST.json" <<'PY'
import json
from pathlib import Path
import sys

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert manifest["schema"] == "ai.neoharness.office.release.v1"
assert manifest["version"] == "9.3.3-nh1"
assert manifest["font_payload"]["identical"] is True
for name in ("viewer", "runtime"):
    labels = manifest["images"][name]["labels"]
    assert labels["org.opencontainers.image.version"] == "9.3.3-nh1"
    assert labels["org.opencontainers.image.revision"] == manifest["source_commit"]
    assert labels["org.opencontainers.image.licenses"] == "AGPL-3.0-only"
PY

evidence="${NHO_VERIFY_EVIDENCE:-${release_root}.verification-$(date -u +%Y%m%dT%H%M%SZ)}"
"${repo}/neoharness/release/run-release-smokes.sh" \
  "${release_root}" "${evidence}"
printf 'release verified; new evidence: %s\n' "${evidence}"
