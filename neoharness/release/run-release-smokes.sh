#!/usr/bin/env bash
set -euo pipefail

readonly VERSION="9.3.3-nh1"
readonly VIEWER_IMAGE="ghcr.io/neoharness/documentserver:${VERSION}"
readonly RUNTIME_IMAGE="ghcr.io/neoharness/office-runtime:${VERSION}"

if [[ $# -ne 2 ]]; then
  printf 'usage: %s RELEASE_ROOT ABSENT_EVIDENCE_DIRECTORY\n' "$0" >&2
  exit 2
fi

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
release_root="$(realpath "$1")"
evidence="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$2")"
if [[ -e "${evidence}" ]]; then
  printf 'refusing to replace evidence path: %s\n' "${evidence}" >&2
  exit 1
fi
mkdir -p "${evidence}"

for command in curl docker pdftotext qpdf; do
  command -v "${command}" >/dev/null || {
    printf 'required smoke command is missing: %s\n' "${command}" >&2
    exit 1
  }
done

mapfile -t packages < <(find "${release_root}/runtime" -maxdepth 1 -type f \
  -name 'neoharness-office-runtime_*_amd64.deb' -print)
if [[ ${#packages[@]} -ne 1 ]]; then
  printf 'expected exactly one amd64 runtime package, found %s\n' \
    "${#packages[@]}" >&2
  exit 1
fi

set -o pipefail
docker run --rm \
  --volume "${release_root}/runtime:/release:ro" \
  debian:13-slim /bin/bash -ceu '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y /release/neoharness-office-runtime_*_amd64.deb
    test "$(cat /opt/neoharness-office/VERSION)" = "9.3.3-nh1"
    nh-office --help
    nh-document --help
    test -s /opt/neoharness-office/share/licenses/LICENSE-FILES.json
    test -s /opt/neoharness-office/share/runtime/quality-policy.v1.sha256
  ' 2>&1 | tee "${evidence}/debian13-package-install.log"

bash "${repo}/neoharness/runtime/tests/run-native-smoke.sh" \
  "${RUNTIME_IMAGE}" "${evidence}/native" \
  2>&1 | tee "${evidence}/native-smoke.log"

container="nho-viewer-smoke-$RANDOM-$$"
viewer_cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap viewer_cleanup EXIT
docker run --detach --name "${container}" \
  --publish 127.0.0.1::80 "${VIEWER_IMAGE}" \
  > "${evidence}/viewer-container-id.txt"
port="$(docker port "${container}" 80/tcp | awk -F: 'NR == 1 {print $NF}')"
test -n "${port}"

ready=false
for _attempt in $(seq 1 180); do
  if ! docker inspect --format '{{.State.Running}}' "${container}" \
      | grep -qx true; then
    break
  fi
  if curl --fail --silent --show-error \
      "http://127.0.0.1:${port}/healthcheck" \
      > "${evidence}/viewer-healthcheck.txt"; then
    ready=true
    break
  fi
  sleep 1
done
docker logs "${container}" > "${evidence}/viewer.log" 2>&1 || true
if [[ "${ready}" != true ]]; then
  printf 'viewer did not reach its health endpoint\n' >&2
  exit 1
fi
viewer_cleanup
trap - EXIT

printf 'package, native engine, and standalone viewer smokes passed\n' \
  | tee "${evidence}/RESULT.txt"
