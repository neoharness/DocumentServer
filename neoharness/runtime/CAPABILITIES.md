# neoHarness Office agent runtime reference

This reference is for selected-agent code running inside an exact document
workspace. It is not a substitute for inspecting the files that are actually
present. A user's desired outcome may be clear even when their description of
the starting state is mistaken.

## Start from observed state

Use `nh-document inspect` on every plausible input before choosing a mutation
method. In particular:

- treat a spreadsheet as a two-dimensional canvas with possibly disconnected
  occupied regions, not as one clean rectangular table;
- distinguish a static Word value from a field or content control;
- inventory slide masters, layouts, notes, charts, media, and placeholders;
- distinguish a born-digital PDF from a scan and OCR only when needed;
- inspect image dimensions, orientation, transparency, animation, color
  profile, and format before placing or converting it.

When the observed state contradicts the user's premise but their intended
outcome is clear and reviewable, implement the outcome and explain the factual
discrepancy. Ask a question when competing interpretations would materially
change the result.

## Choose by preservation and publication policy

The final writer is deterministic; it is not a model preference.

1. Create DOCX, XLSX, PPTX, Visio OOXML, and native editable PDF deliverables
   with `nh-office run` and the neoHarness Office API.
2. Revise an existing OOXML file with either:
   - `nh-office run` when the change requires editor semantics; or
   - `nh-document ooxml` when its bounded operation expresses the entire
     requested change and its package comparison proves the unrelated parts
     were preserved.

For existing workbooks, prefer `nh-document ooxml xlsx-cells` when the entire
request is an exact set of cell value or formula changes, and
`nh-document ooxml xlsx-format` for bounded cell styling, row/column sizing,
panes, gridlines, tab color, or sheet visibility. Use `nh-office run` when the
outcome requires native editor semantics such as new charts, drawings,
validation, tables, or workbook structure. Do not learn this boundary by
round-tripping a fragile workbook first: inspect the source and choose the
narrowest operation that fully expresses the requested outcome.
3. Use the domain finalizer for bounded non-Office work: qpdf for PDF page and
   structural operations, OCRmyPDF/Tesseract for searchable PDF creation, and
   the image pipeline for image transformations.
4. Python, Pandoc, LibreOffice, python-docx, openpyxl, python-pptx, and direct
   XML remain available for inspection, extraction, planning, intermediate
   conversion, and recovery. They do **not** qualify the final bytes of a new
   or revised OOXML deliverable. Do not choose one merely because it can make
   a file that opens.

Opening a lossy intermediate in neoHarness Office does not restore structures,
formatting, metadata, or layout that an earlier serializer discarded. If the
approved finalizer cannot produce the requested result, retain the source and
report that bounded failure; do not silently downgrade the user's file.

Every approved writer emits artifact-bound provenance under the immutable
`neoharness-office-quality.v1` policy. Publication must use the provenance
captured by the server from that exact execution and verify the output hash.
A JSON claim supplied by selected-agent code is not publication authority.
All utilities remain callable; the policy governs which exact bytes qualify as
a user-facing deliverable.

In the disposable utility image, invoke `nh-office` and `nh-document` from the
normal `PATH`; those names are attested clients backed by the root-owned local
finalizer broker. Do not call `/opt/neoharness-office/bin/nh-office` or
`/opt/neoharness-office/bin/nh-document` directly. Direct invocation remains
available for package recovery but intentionally produces no server
attestation and therefore cannot be published.

Every Office/PDF source passed to a finalizer must be declared as an input.
The gateway seals canonical `/workspace/input` bytes before selected-agent
execution begins, and the broker also recognizes unchanged outputs from prior
approved finalizers. It rejects unsealed working-document inputs, including a
lossy serializer's output reopened only to acquire native provenance.

Raw, version-matched API Builder sources are installed at:

```text
/opt/neoharness-office/share/api-reference/word-apiBuilder.js
/opt/neoharness-office/share/api-reference/cell-apiBuilder.js
/opt/neoharness-office/share/api-reference/slide-apiBuilder.js
/opt/neoharness-office/share/api-reference/pdf-apiBuilder.js
```

Search those files for exact method signatures rather than guessing an API.
Useful entry points include:

- Word: `SearchAndReplace`, `Search`, `GetAllContentControls`,
  `GetContentControlsByTag`, `GetAllStyles`, `GetSections`, `GetHeader`,
  `GetFooter`, `GetAllTablesOnPage`, `ReplaceDrawing`, and field/form APIs.
- Spreadsheet: `GetRange`, `SetValue`, `SetNumberFormat`, `FormatAsTable`,
  `AddChart`, `AddImage`, `AddDefName`, `GetValidation`, `GetFormatConditions`,
  drawings, pivot tables, and protected ranges.
- Presentation: slide/master/layout enumeration, placeholders, images, charts,
  tables, drawings, speaker notes, and animation timelines.
- PDF: pages, annotations, form fields, drawings, rotation, search, redaction,
  and export.

## Images and cross-format work

Canonical image bytes can enter `/workspace/input` alongside Office and PDF
files. Use `nh-document image` for orientation, crop, content trim, contain or
cover fitting, conversion, transparency flattening, and exact metadata. Native
Office image methods accept a data URL; read the local image bytes and encode
them rather than assuming a public URL exists.

One workspace may contain multiple mutable files and read-only references.
Use distinct intermediate, output, and manifest paths for every independent
operation. Several `nh-office` processes may run concurrently when their paths
do not overlap. Settle outputs independently: one failed sibling must not erase
or replay successful siblings.

A representative cross-format operation can therefore:

1. inspect a PDF, DOCX, XLSX, and several images;
2. OCR only the scanned PDF pages;
3. extract or crop an image;
4. read a disconnected table region from the workbook;
5. place both into a Word file using the native API;
6. save the editable DOCX and render a PDF as independent outputs;
7. compare package and page renderings before publication.

## Preservation and verification

- Macro-bearing packages may be inspected and round-tripped; never execute
  their macros. Verify that each `vbaProject.bin` remains byte-identical.
- Do not fetch external OOXML relationships unless the user explicitly
  authorized that retrieval.
- Compare exact package parts after a surgical change.
- Render the relevant before/after pages when layout matters.
- Validate the requested semantic change separately from visual fidelity.
- Keep successful artifacts even when a sibling operation fails.
- Treat rendering as evidence, not as permission to replace an editable source
  with a visually similar but structurally degraded reconstruction.
