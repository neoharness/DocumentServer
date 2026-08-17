# Reproducing neoHarness Office 9.3.3-nh1

This release is built from the recursive source graph recorded in
`NEOHARNESS-RELEASE/SOURCE-MANIFEST.json`. Do not substitute branch tips for
the recorded commits.

## Host prerequisites

- amd64 Linux with Docker Engine and the Buildx plugin
- Git, Python 3, GNU tar, zstd, curl, jq, and sha256sum
- enough local storage for the source tree, BuildKit cache, packages, images,
  SBOMs, and corresponding-source archive

The reference build used Ubuntu 24.04 and a dedicated unprivileged build user
with Docker access. Network retrieval occurs only while resolving the pinned
build inputs and OS packages.

## Checkout

```sh
git clone --recurse-submodules https://github.com/neoharness/DocumentServer.git
cd DocumentServer
git checkout <source_commit_from_SOURCE-MANIFEST.json>
git submodule sync --recursive
git submodule update --init --recursive
git submodule foreach --recursive 'test "$(git rev-parse HEAD)" = "$sha1"'
```

The release command refuses dirty repositories and any submodule whose checked
out commit differs from its recorded gitlink.

## Build

Install the pinned SBOM generator without piping remote code into a shell:

```sh
./neoharness/release/install-syft.sh "$PWD/.release-tools/bin"
```

Then build all release products:

```sh
export NHO_BUILD_CACHE_ROOT=/absolute/path/to/persistent/buildx-cache
./neoharness/release/build-release.sh /absolute/path/to/new/output-directory
```

The output directory must not already exist. The script builds and records:

- `ghcr.io/neoharness/documentserver:9.3.3-nh1`
- `ghcr.io/neoharness/office-runtime:9.3.3-nh1`
- the runtime `.deb` and deterministic `tar.zst` bundle
- OCI archives for both images
- a recursive corresponding-source archive
- exact image/source identities, checksums, and provenance
- SPDX JSON and CycloneDX JSON SBOMs for source and both images

BuildKit caches are performance inputs only. Removing them must not change the
declared source coordinates or release identity.

## Mechanical verification

The build itself executes a source-built native DOCX packaging smoke and the
runtime helper/finalizer tests. After the build, run:

```sh
./neoharness/release/verify-release.sh /absolute/path/to/output-directory
```

This verifies checksums, image labels, package installation in a clean Debian
13 container, native DOCX/XLSX/PPTX/PDF operations, and the shared viewer/runtime
font payload digest. These are native mechanical proofs; they are not a claim
of universal real-world document fidelity.

## Installation

On a compatible Debian-family utility image:

```sh
apt-get update
apt-get install -y ./neoharness-office-runtime_9.3.3-nh1_amd64.deb
nh-office --help
nh-document --help
```

The runtime package intentionally omits PostgreSQL, Redis, RabbitMQ, Nginx, the
browser editor, and collaboration services.
