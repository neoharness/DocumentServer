#!/usr/bin/env bash
set -euo pipefail

readonly VERSION="9.3.3-nh1"
readonly VIEWER_IMAGE="ghcr.io/neoharness/documentserver:${VERSION}"
readonly RUNTIME_IMAGE="ghcr.io/neoharness/office-runtime:${VERSION}"

if [[ $# -ne 1 ]]; then
  printf 'usage: %s ABSENT_OUTPUT_DIRECTORY\n' "$0" >&2
  exit 2
fi

repo="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
requested_output="$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1")"
if [[ -e "${requested_output}" ]]; then
  printf 'refusing to replace existing release path: %s\n' "${requested_output}" >&2
  exit 1
fi
output_parent="$(dirname "${requested_output}")"
mkdir -p "${output_parent}"

for command in docker git jq python3 sha256sum tar zstd; do
  command -v "${command}" >/dev/null || {
    printf 'required command is missing: %s\n' "${command}" >&2
    exit 1
  }
done

syft="${NHO_SYFT:-/srv/neoharness-office/tools/bin/syft}"
if [[ ! -x "${syft}" ]]; then
  printf 'pinned syft executable is missing: %s\n' "${syft}" >&2
  exit 1
fi

if [[ -n "$(git -C "${repo}" status --porcelain --untracked-files=all)" ]]; then
  printf 'release source tree is dirty\n' >&2
  git -C "${repo}" status --short >&2
  exit 1
fi
git -C "${repo}" submodule foreach --quiet --recursive \
  'test "$(git rev-parse HEAD)" = "$sha1" && test -z "$(git status --porcelain --untracked-files=all)"'

source_commit="$(git -C "${repo}" rev-parse HEAD)"
source_epoch="$(git -C "${repo}" show -s --format=%ct HEAD)"
short_revision="$(git -C "${repo}" rev-parse --short=12 HEAD)"
cache_root="${NHO_BUILD_CACHE_ROOT:-/srv/neoharness-office/cache/buildx}"
mkdir -p "${cache_root}"

stage="$(mktemp -d --tmpdir="${output_parent}" .nho-release-${VERSION}.XXXXXXXX)"
cleanup() {
  rm -rf -- "${stage}"
}
trap cleanup EXIT
mkdir -p "${stage}/runtime" "${stage}/images" "${stage}/source" \
  "${stage}/sbom" "${stage}/evidence"

export REGISTRY="ghcr.io/neoharness"
export TAG="${VERSION}"
export PRODUCT_VERSION="9.3.3"
export NHO_RUNTIME_VERSION="${VERSION}"
export NHO_SOURCE_REVISION="${source_commit}"
export SOURCE_DATE_EPOCH="${source_epoch}"
export BUILD_NUMBER="nh1"
export DS_VERSION_HASH="${short_revision}"
export WEB_APPS_THEME="whitelabel"
export BUILD_CACHE_ROOT="${cache_root}"
export CACHE_NAMESPACE="neoharness"

bake=(
  docker buildx bake
  --builder "${NHO_BUILDX_BUILDER:-nho-builder}"
  --allow="fs.read=${repo}"
  --allow="fs=${cache_root}"
  --progress=plain
)

(
  cd "${repo}/build"
  "${bake[@]}" headless-runtime-artifacts \
    --set "headless-runtime-artifacts.output=type=local,dest=${stage}/runtime"
  "${bake[@]}" --load headless-runtime-oci
  "${bake[@]}" --load standalone

  "${bake[@]}" --provenance=mode=max headless-runtime-oci \
    --set "headless-runtime-oci.output=type=oci,dest=${stage}/images/neoharness-office-runtime_${VERSION}_amd64.oci.tar"
  "${bake[@]}" --provenance=mode=max standalone \
    --set "standalone.output=type=oci,dest=${stage}/images/neoharness-documentserver_${VERSION}_amd64.oci.tar"
)

zstd -19 -T0 --rm "${stage}/images/neoharness-office-runtime_${VERSION}_amd64.oci.tar"
zstd -19 -T0 --rm "${stage}/images/neoharness-documentserver_${VERSION}_amd64.oci.tar"

"${repo}/neoharness/release/run-release-smokes.sh" \
  "${stage}" "${stage}/evidence/release-smokes"

python3 "${repo}/neoharness/release/generate-source-release.py" \
  --repo "${repo}" --version "${VERSION}" \
  --output "${stage}/source/neoharness-office-${VERSION}-source.tar.zst"

source_scan="$(mktemp -d --tmpdir="${stage}" source-scan.XXXXXXXX)"
tar --use-compress-program=unzstd -xf \
  "${stage}/source/neoharness-office-${VERSION}-source.tar.zst" \
  -C "${source_scan}"
source_tree="${source_scan}/neoharness-office-${VERSION}-source"

"${syft}" scan "dir:${source_tree}" \
  -o "spdx-json=${stage}/sbom/source.spdx.json" \
  -o "cyclonedx-json=${stage}/sbom/source.cyclonedx.json"
"${syft}" scan "docker:${VIEWER_IMAGE}" \
  -o "spdx-json=${stage}/sbom/viewer.spdx.json" \
  -o "cyclonedx-json=${stage}/sbom/viewer.cyclonedx.json"
"${syft}" scan "docker:${RUNTIME_IMAGE}" \
  -o "spdx-json=${stage}/sbom/runtime.spdx.json" \
  -o "cyclonedx-json=${stage}/sbom/runtime.cyclonedx.json"
rm -rf -- "${source_scan}"

font_digest() {
  local image="$1"
  local path="$2"
  docker run --rm --entrypoint /bin/sh "${image}" -ceu \
    'cd "$1"; find . -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -d" " -f1' \
    sh "${path}"
}

runtime_font_digest="$(font_digest "${RUNTIME_IMAGE}" \
  /opt/neoharness-office/documentserver/core-fonts)"
viewer_font_digest="$(font_digest "${VIEWER_IMAGE}" \
  /var/www/euro-office/documentserver/core-fonts)"

cp "${repo}/neoharness/release/REPRODUCE.md" "${stage}/REPRODUCE.md"
cp "${repo}/neoharness/release/THIRD_PARTY_NOTICES.md" \
  "${stage}/THIRD_PARTY_NOTICES.md"
cp "${repo}/LICENSE" "${stage}/AGPL-3.0.txt"

python3 "${repo}/neoharness/release/generate-release-manifest.py" \
  --release-root "${stage}" \
  --version "${VERSION}" \
  --source-commit "${source_commit}" \
  --source-date-epoch "${source_epoch}" \
  --viewer-image "${VIEWER_IMAGE}" \
  --runtime-image "${RUNTIME_IMAGE}" \
  --viewer-font-digest "${viewer_font_digest}" \
  --runtime-font-digest "${runtime_font_digest}"

mv "${stage}" "${requested_output}"
trap - EXIT
printf 'neoHarness Office %s release written to %s\n' \
  "${VERSION}" "${requested_output}"
