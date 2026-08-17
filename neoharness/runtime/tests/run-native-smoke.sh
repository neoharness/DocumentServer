#!/bin/sh
set -eu

image="${1:?usage: run-native-smoke.sh IMAGE WORKSPACE}"
workspace="${2:?usage: run-native-smoke.sh IMAGE WORKSPACE}"
workspace="$(realpath "${workspace}")"
script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
fixture_dir="${script_dir}/fixtures/native"

mkdir -p "${workspace}/input" "${workspace}/output"
mkdir -p "${workspace}/work"
cp "${fixture_dir}"/*.js "${workspace}/work/"

run_office()
{
    docker run --rm \
        --volume "${workspace}:/workspace" \
        "${image}" run "$@"
}

run_office /workspace/work/00-create-docx.js \
    --output /workspace/output/runtime-original.docx \
    --manifest /workspace/output/00-create-docx.json
cp "${workspace}/output/runtime-original.docx" \
    "${workspace}/input/runtime-original.docx"
run_office /workspace/work/01-edit-docx.js \
    --input /workspace/input/runtime-original.docx \
    --output /workspace/output/runtime-edited.docx \
    --manifest /workspace/output/01-edit-docx.json

run_office /workspace/work/10-create-xlsx.js \
    --output /workspace/output/runtime-original.xlsx \
    --manifest /workspace/output/10-create-xlsx.json
cp "${workspace}/output/runtime-original.xlsx" \
    "${workspace}/input/runtime-original.xlsx"
run_office /workspace/work/11-edit-xlsx.js \
    --input /workspace/input/runtime-original.xlsx \
    --output /workspace/output/runtime-edited.xlsx \
    --manifest /workspace/output/11-edit-xlsx.json

run_office /workspace/work/20-create-pptx.js \
    --output /workspace/output/runtime-original.pptx \
    --manifest /workspace/output/20-create-pptx.json
cp "${workspace}/output/runtime-original.pptx" \
    "${workspace}/input/runtime-original.pptx"
run_office /workspace/work/21-edit-pptx.js \
    --input /workspace/input/runtime-original.pptx \
    --output /workspace/output/runtime-edited.pptx \
    --manifest /workspace/output/21-edit-pptx.json

cp "${workspace}/output/runtime-edited.docx" \
    "${workspace}/input/runtime-multi-source.docx"
cp "${workspace}/output/runtime-edited.xlsx" \
    "${workspace}/input/runtime-multi-source.xlsx"
run_office /workspace/work/25-edit-multiple.js \
    --input /workspace/input/runtime-multi-source.docx \
    --input /workspace/input/runtime-multi-source.xlsx \
    --output /workspace/output/runtime-multi-edited.docx \
    --output /workspace/output/runtime-multi-edited.xlsx \
    --manifest /workspace/output/25-edit-multiple.json

run_office /workspace/work/30-create-pdf.js \
    --output /workspace/output/runtime-original.pdf \
    --manifest /workspace/output/30-create-pdf.json
cp "${workspace}/output/runtime-original.pdf" \
    "${workspace}/input/runtime-original.pdf"

for format in docx xlsx pptx; do
    cp "${workspace}/output/runtime-edited.${format}" \
        "${workspace}/input/runtime-edited.${format}"
    run_office /workspace/work/40-convert-pdf.js \
        --input "/workspace/input/runtime-edited.${format}" \
        --output "/workspace/output/runtime-edited-${format}.pdf" \
        --manifest "/workspace/output/40-convert-${format}.json"
done

run_office /workspace/work/31-edit-pdf.js \
    --input /workspace/input/runtime-original.pdf \
    --output /workspace/output/runtime-edited.pdf \
    --manifest /workspace/output/31-edit-pdf.json

qpdf --check "${workspace}/output/runtime-edited.pdf"
if cmp -s "${workspace}/output/runtime-original.pdf" \
    "${workspace}/output/runtime-edited.pdf"; then
    echo "existing-PDF edit returned unchanged bytes" >&2
    exit 1
fi

pdftotext "${workspace}/output/runtime-original.pdf" \
    "${workspace}/output/runtime-original.txt"
pdftotext "${workspace}/output/runtime-edited.pdf" \
    "${workspace}/output/runtime-edited.txt"
grep -Fq 'NHO_RUNTIME_PDF_ORIGINAL' \
    "${workspace}/output/runtime-edited.txt"
grep -Fq 'NHO_RUNTIME_PDF_EDITED' \
    "${workspace}/output/runtime-edited.txt"

printf 'native smoke complete; existing-PDF edit passed\n'
