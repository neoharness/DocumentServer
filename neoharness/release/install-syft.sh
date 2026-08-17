#!/usr/bin/env bash
set -euo pipefail

readonly SYFT_VERSION="1.50.0"
readonly SYFT_ARCHIVE="syft_${SYFT_VERSION}_linux_amd64.tar.gz"
readonly SYFT_SHA256="bf7b29ff57f06da30918266a0e1c2885a8f99784798d1bdb1628886aa015d788"
readonly SYFT_URL="https://github.com/anchore/syft/releases/download/v${SYFT_VERSION}/${SYFT_ARCHIVE}"

destination="${1:-/srv/neoharness-office/tools/bin}"
mkdir -p "${destination}"

temporary="$(mktemp -d)"
trap 'rm -rf -- "${temporary}"' EXIT

curl --fail --location --proto '=https' --tlsv1.2 \
  --output "${temporary}/${SYFT_ARCHIVE}" "${SYFT_URL}"
printf '%s  %s\n' "${SYFT_SHA256}" "${temporary}/${SYFT_ARCHIVE}" \
  | sha256sum --check --strict
tar -xzf "${temporary}/${SYFT_ARCHIVE}" -C "${temporary}" syft
install -m 0755 "${temporary}/syft" "${destination}/syft"

actual="$(${destination}/syft version -o json | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["version"])')"
test "${actual}" = "${SYFT_VERSION}"
printf 'Installed syft %s at %s\n' "${actual}" "${destination}/syft"
