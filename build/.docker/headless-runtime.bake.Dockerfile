# syntax=docker/dockerfile:1.7

ARG BUILD_ROOT=/package
ARG NHO_RUNTIME_VERSION=9.3.3-nh1
ARG NHO_SOURCE_REVISION=unknown
ARG SOURCE_DATE_EPOCH=0

FROM ubuntu:24.04 AS payload

ARG BUILD_ROOT

ENV DEBIAN_FRONTEND=noninteractive
ENV NHO_ROOT=/opt/neoharness-office/documentserver

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates file ghostscript imagemagick img2pdf jpegoptim \
        libheif-examples libimage-exiftool-perl libraw-bin librsvg2-bin \
        libvips-tools ocrmypdf optipng pandoc pngquant poppler-utils \
        python3 python3-docx python3-lxml python3-openpyxl python3-pikepdf \
        python3-pil qpdf tesseract-ocr tesseract-ocr-eng unpaper unzip webp zip \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p \
        /opt/neoharness-office/documentserver/server/FileConverter/bin \
        /opt/neoharness-office/documentserver/server/FileConverter/lib \
        /opt/neoharness-office/documentserver/server/tools \
        /opt/neoharness-office/share/api-reference \
        /opt/neoharness-office/share/licenses \
        /opt/neoharness-office/share/runtime \
        /opt/neoharness-office/share/systemd

# Source-built native engine and tools. These named build contexts are wired to
# the existing core and sdkjs Buildx targets by build/docker-bake.hcl.
COPY --from=core ${BUILD_ROOT}/bin/ \
    /opt/neoharness-office/documentserver/server/FileConverter/bin/
COPY --from=core ${BUILD_ROOT}/tools/ \
    /opt/neoharness-office/documentserver/server/tools/
COPY --from=core ${BUILD_ROOT}/*.so* \
    /opt/neoharness-office/documentserver/server/FileConverter/lib/
COPY --from=core ${BUILD_ROOT}/tools/*.so* \
    /opt/neoharness-office/documentserver/server/tools/
COPY --from=sdkjs ${BUILD_ROOT}/ \
    /opt/neoharness-office/documentserver/

# Native Document Builder resources. Keep the upstream relative layout because
# DoctRenderer.config resolves SDKJS, dictionaries, and xregexp from the
# FileConverter/bin directory.
COPY core-fonts/ \
    /opt/neoharness-office/documentserver/core-fonts/
COPY dictionaries/ \
    /opt/neoharness-office/documentserver/dictionaries/
COPY document-templates/ \
    /opt/neoharness-office/documentserver/document-templates/
COPY web-apps/vendor/xregexp/xregexp-all-min.js \
    /opt/neoharness-office/documentserver/web-apps/vendor/xregexp/xregexp-all-min.js
COPY build/configs/core/DoctRenderer.config \
    /opt/neoharness-office/documentserver/server/FileConverter/bin/DoctRenderer.config

# Deterministic asset initializer and notices.
COPY --chmod=755 neoharness/runtime/scripts/generate-runtime-assets.sh \
    /usr/local/bin/generate-runtime-assets
COPY neoharness/runtime/README.md \
    /opt/neoharness-office/share/runtime/README.md
COPY neoharness/runtime/CAPABILITIES.md \
    /opt/neoharness-office/share/runtime/CAPABILITIES.md
COPY neoharness/runtime/quality-policy.v1.json \
    /opt/neoharness-office/share/runtime/quality-policy.v1.json
COPY neoharness/release/THIRD_PARTY_NOTICES.md \
    /opt/neoharness-office/share/licenses/THIRD_PARTY_NOTICES.md
COPY neoharness/release/collect-runtime-licenses.py \
    /usr/local/libexec/collect-runtime-licenses
COPY sdkjs/word/apiBuilder.js \
    /opt/neoharness-office/share/api-reference/word-apiBuilder.js
COPY sdkjs/cell/apiBuilder.js \
    /opt/neoharness-office/share/api-reference/cell-apiBuilder.js
COPY sdkjs/slide/apiBuilder.js \
    /opt/neoharness-office/share/api-reference/slide-apiBuilder.js
COPY sdkjs/pdf/apiBuilder.js \
    /opt/neoharness-office/share/api-reference/pdf-apiBuilder.js
COPY LICENSE /opt/neoharness-office/share/licenses/AGPL-3.0.txt
COPY core/LICENSE.txt /opt/neoharness-office/share/licenses/core-LICENSE.txt
COPY sdkjs/LICENSE.txt /opt/neoharness-office/share/licenses/sdkjs-LICENSE.txt
RUN --mount=type=bind,source=.,target=/source,ro \
    python3 /usr/local/libexec/collect-runtime-licenses \
        /source /opt/neoharness-office/share/licenses

# Keep release identity below dependency installation and static runtime assets
# so a new source revision does not invalidate those expensive layers.
ARG NHO_RUNTIME_VERSION
ARG NHO_SOURCE_REVISION
ARG SOURCE_DATE_EPOCH

RUN printf '%s\n' "${NHO_RUNTIME_VERSION}" \
        > /opt/neoharness-office/VERSION \
    && printf '%s\n' "${NHO_SOURCE_REVISION}" \
        > /opt/neoharness-office/SOURCE_REVISION \
    && sha256sum /opt/neoharness-office/share/runtime/quality-policy.v1.json \
        | awk '{print $1}' \
        > /opt/neoharness-office/share/runtime/quality-policy.v1.sha256 \
    && generate-runtime-assets /opt/neoharness-office/documentserver

# Keep launcher-only changes downstream from the comparatively expensive font,
# theme, and JavaScript-cache generation layer.
COPY --chmod=755 neoharness/runtime/bin/nh-office \
    /opt/neoharness-office/bin/nh-office
COPY --chmod=755 neoharness/runtime/bin/nh-document \
    /opt/neoharness-office/bin/nh-document
COPY --chmod=755 neoharness/runtime/bin/nh-attested \
    /opt/neoharness-office/bin/nh-attested
COPY --chmod=755 neoharness/runtime/bin/nh-finalizerd \
    /opt/neoharness-office/bin/nh-finalizerd
COPY neoharness/runtime/python/neoharness_office/ \
    /opt/neoharness-office/lib/python3/neoharness_office/
COPY neoharness/runtime/systemd/nh-office-finalizerd.service \
    /opt/neoharness-office/share/systemd/nh-office-finalizerd.service
RUN ln -s /opt/neoharness-office/bin/nh-office /usr/bin/nh-office \
    && ln -s /opt/neoharness-office/bin/nh-document /usr/bin/nh-document \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-office-attested \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-document-attested \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-input-attest \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-artifact-qualify \
    && find /opt/neoharness-office /usr/bin/nh-office \
        /usr/bin/nh-document /usr/bin/nh-office-attested \
        /usr/bin/nh-document-attested /usr/bin/nh-input-attest \
        /usr/bin/nh-artifact-qualify \
        -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +

# Prove the assembled filesystem can execute the source-built engine before it
# is wrapped as a package or image. This is a packaging smoke, not the broader
# compatibility corpus.
RUN mkdir -p /workspace/input /workspace/work /workspace/output \
    && chmod 0755 /workspace/input /workspace/work /workspace/output
COPY neoharness/runtime/tests/package-smoke.js /workspace/work/package-smoke.js
COPY neoharness/runtime/tests/test_document_helpers.py \
    /workspace/work/test_document_helpers.py
COPY neoharness/runtime/tests/test_attestation.py \
    /workspace/work/test_attestation.py
RUN /opt/neoharness-office/bin/nh-office run \
        /workspace/work/package-smoke.js \
        --output /workspace/output/package-smoke.docx \
        --manifest /workspace/output/package-smoke-manifest.json \
    && unzip -t /workspace/output/package-smoke.docx \
    && grep -q '"status": "succeeded"' \
        /workspace/output/package-smoke-manifest.json \
    && /opt/neoharness-office/bin/nh-document quality qualify \
        /workspace/output/package-smoke.docx \
        --evidence /workspace/output/package-smoke-manifest.json \
        --intent create \
        --family ooxml \
        --json-output /workspace/output/package-smoke-qualification.json \
    && grep -q '"status": "qualified"' \
        /workspace/output/package-smoke-qualification.json \
    && test "$(stat -c %s \
        /opt/neoharness-office/documentserver/server/FileConverter/bin/AllFonts.js)" \
        -gt 1024 \
    && test "$(stat -c %s \
        /opt/neoharness-office/documentserver/server/FileConverter/bin/font_selection.bin)" \
        -gt 1024 \
    && test "$(stat -c %s \
        /opt/neoharness-office/share/api-reference/word-apiBuilder.js)" \
        -gt 100000 \
    && test "$(stat -c %s \
        /opt/neoharness-office/share/api-reference/cell-apiBuilder.js)" \
        -gt 100000 \
    && test "$(stat -c %s \
        /opt/neoharness-office/share/api-reference/slide-apiBuilder.js)" \
        -gt 100000 \
    && test "$(stat -c %s \
        /opt/neoharness-office/share/api-reference/pdf-apiBuilder.js)" \
        -gt 100000 \
    && PYTHONPATH=/opt/neoharness-office/lib/python3 \
        python3 -m unittest -v \
            /workspace/work/test_document_helpers.py \
            /workspace/work/test_attestation.py

FROM ubuntu:24.04 AS artifacts

ARG NHO_RUNTIME_VERSION
ARG SOURCE_DATE_EPOCH
ARG TARGETARCH

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        dpkg-dev zstd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=payload /opt/neoharness-office/ /rootfs/opt/neoharness-office/
RUN mkdir -p /rootfs/usr/bin /rootfs/lib/systemd/system \
    && ln -s /opt/neoharness-office/bin/nh-office \
        /rootfs/usr/bin/nh-office \
    && ln -s /opt/neoharness-office/bin/nh-document \
        /rootfs/usr/bin/nh-document \
    && ln -s /opt/neoharness-office/bin/nh-attested \
        /rootfs/usr/bin/nh-office-attested \
    && ln -s /opt/neoharness-office/bin/nh-attested \
        /rootfs/usr/bin/nh-document-attested \
    && ln -s /opt/neoharness-office/bin/nh-attested \
        /rootfs/usr/bin/nh-input-attest \
    && ln -s /opt/neoharness-office/bin/nh-attested \
        /rootfs/usr/bin/nh-artifact-qualify \
    && cp /rootfs/opt/neoharness-office/share/systemd/nh-office-finalizerd.service \
        /rootfs/lib/systemd/system/nh-office-finalizerd.service

RUN set -eux; \
    mkdir -p /pkgroot/DEBIAN /out; \
    cp -a /rootfs/. /pkgroot/; \
    installed_size="$(du -sk /rootfs | awk '{print $1}')"; \
    { \
      echo 'Package: neoharness-office-runtime'; \
      echo "Version: ${NHO_RUNTIME_VERSION}"; \
      echo "Architecture: ${TARGETARCH}"; \
      echo 'Maintainer: neoHarness <opensource@neoharness.ai>'; \
      echo 'Depends: libc6 (>= 2.35), libgcc-s1, libstdc++6, python3 (>= 3.11), file, ghostscript, imagemagick, img2pdf, jpegoptim, libheif-examples, libimage-exiftool-perl, libraw-bin, librsvg2-bin, libvips-tools, ocrmypdf, optipng, pandoc, pngquant, poppler-utils, python3-docx, python3-lxml, python3-openpyxl, python3-pikepdf, python3-pil, qpdf, tesseract-ocr, tesseract-ocr-eng, unpaper, unzip, util-linux, webp, zip'; \
      echo "Installed-Size: ${installed_size}"; \
      echo 'Section: editors'; \
      echo 'Priority: optional'; \
      echo 'Homepage: https://neoharness.ai/opensource/'; \
      echo 'Description: neoHarness Office source-built headless document runtime'; \
      echo ' Native DOCX, XLSX, PPTX, PDF, image, conversion, and inspection assets'; \
      echo ' without the DocumentServer web, database, broker, or collaboration stack.'; \
    } > /pkgroot/DEBIAN/control; \
    find /pkgroot -exec touch -h -d "@${SOURCE_DATE_EPOCH}" {} +; \
    dpkg-deb --root-owner-group --build /pkgroot \
      "/out/neoharness-office-runtime_${NHO_RUNTIME_VERSION}_${TARGETARCH}.deb"; \
    tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" \
      --owner=0 --group=0 --numeric-owner -C /rootfs -cf - . \
      | zstd -19 -T0 \
      > "/out/neoharness-office-runtime_${NHO_RUNTIME_VERSION}_${TARGETARCH}.tar.zst"; \
    cd /out; \
    sha256sum neoharness-office-runtime_* > SHA256SUMS

FROM scratch AS headless-runtime-artifacts
COPY --from=artifacts /out/ /

FROM ubuntu:24.04 AS headless-runtime-oci

ARG NHO_RUNTIME_VERSION
ARG NHO_SOURCE_REVISION

ENV DEBIAN_FRONTEND=noninteractive
ENV NHO_INSTALL_ROOT=/opt/neoharness-office

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates file ghostscript imagemagick img2pdf jpegoptim \
        libheif-examples libimage-exiftool-perl libraw-bin librsvg2-bin \
        libvips-tools ocrmypdf optipng pandoc pngquant poppler-utils \
        python3 python3-docx python3-lxml python3-openpyxl python3-pikepdf \
        python3-pil qpdf tesseract-ocr tesseract-ocr-eng unpaper unzip webp zip \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /workspace/input /workspace/work /workspace/output

COPY --from=payload /opt/neoharness-office/ /opt/neoharness-office/
RUN ln -s /opt/neoharness-office/bin/nh-office /usr/bin/nh-office \
    && ln -s /opt/neoharness-office/bin/nh-document /usr/bin/nh-document \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-office-attested \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-document-attested \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-input-attest \
    && ln -s /opt/neoharness-office/bin/nh-attested /usr/bin/nh-artifact-qualify

LABEL org.opencontainers.image.title="neoHarness Office headless runtime" \
      org.opencontainers.image.version="${NHO_RUNTIME_VERSION}" \
      org.opencontainers.image.revision="${NHO_SOURCE_REVISION}" \
      org.opencontainers.image.source="https://github.com/neoharness/DocumentServer" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      ai.neoharness.office.runtime="headless"

WORKDIR /workspace/work
ENTRYPOINT ["/usr/bin/nh-office"]
CMD ["--help"]
