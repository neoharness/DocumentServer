# neoHarness Office headless runtime

This package contains the source-built Euro-Office native document engine and
the SDKJS resources required to run it without DocumentServer's web,
collaboration, database, cache, broker, or proxy services.

The supported entry point is intentionally thin:

```sh
nh-office run /workspace/work/edit.js \
  --input /workspace/input/source.docx \
  --output /workspace/output/candidate.docx \
  --argument /workspace/work/argument.json
```

The JavaScript program receives a global `Argument` object:

```json
{
  "input": "/workspace/input/source.docx",
  "inputs": ["/workspace/input/source.docx"],
  "output": "/workspace/output/candidate.docx",
  "outputs": ["/workspace/output/candidate.docx"],
  "argument": {}
}
```

Repeated `--input` and `--output` options populate the `inputs` and `outputs`
arrays. `builder.CloseFile()` resets Document Builder's JavaScript context. A
multi-document program must therefore read the next array coordinate from the
re-initialized `Argument` object after each close rather than carrying local
variables across that boundary.

Document Builder requires dynamic paths to use its `jsValue(...)` convention:

```javascript
var inputPath = Argument["input"];
var outputPath = Argument["output"];
builder.OpenFile("jsValue(inputPath)");
// Make the requested change through the native Office API.
builder.SaveFile("docx", "jsValue(outputPath)");
builder.CloseFile();
```

Every declared output is checked as a regular no-follow file beneath
`/workspace/output`. The launcher writes a bounded JSON result manifest with
the engine status, diagnostic byte counts, artifact size, MIME type, and
SHA-256 digest. Each artifact also carries `native_office` finalizer provenance
bound to that exact digest and the installed quality-policy digest. Successful
sibling artifacts remain described even if another declared output fails.

`nh-document inspect` inventories the file that actually exists instead of
assuming the user's description of its starting state is accurate. For OOXML
it reports package preservation facts and format-specific structure, including
spatial regions in irregular spreadsheets. For PDFs it reports text/image/font
evidence and whether the sampled pages appear scanned. `nh-document compare`
reports exact changed, added, and removed package parts, including whether a
macro payload remained byte-identical.

The adaptive layer also provides bounded operations for the places where a
generic library is likely to flatten or reinterpret an irregular real-world
file:

```sh
# Replace text split across Word runs without rebuilding the package.
nh-document ooxml replace-text /workspace/input/source.docm \
  /workspace/output/candidate.docm \
  --search 'Old company name' --replacement 'New company name' \
  --expected-count 1

# Replace an observed static date with a real Word field. The command does not
# assume the user's claim that a field already exists.
nh-document ooxml word-field /workspace/input/source.docx \
  /workspace/output/candidate.docx \
  --search 'August 17, 2026' \
  --instruction 'DATE \\@ "MMMM d, yyyy"' --expected-count 1

# Apply exact, disconnected cell changes to a free-form workbook. The JSON
# plan maps sheet names to A1 coordinates. Every successful change requests a
# full recalculation on open so dependent cached values cannot remain stale.
nh-document ooxml xlsx-cells /workspace/input/source.xlsm \
  /workspace/output/candidate.xlsm \
  --plan /workspace/work/cell-plan.json
```

The cell plan shape is explicit:

```json
{
  "Executive": {
    "B2": 41,
    "C7": {"formula": "=B2+1", "cached": 42},
    "D8": {"value": "=literal text"},
    "F9": {"value": "New text", "style_from": "F8"},
    "G10": {"clear": true}
  }
}
```

A scalar string beginning with `=` is rejected as ambiguous. Use the explicit
`formula` member for a formula, or the explicit `value` member for literal text
that begins with an equals sign.

For presentation-only workbook changes without an editor-wide round trip, use
the surgical formatter:

```bash
nh-document ooxml xlsx-format /workspace/input/source.xlsx \
  /workspace/output/formatted.xlsx \
  --plan /workspace/work/format-plan.json
```

The format plan maps worksheet names to bounded `ranges`, `columns`, `rows`,
`freeze_panes`, `show_gridlines`, `tab_color`, and `state`. Range rules support
font, fill, border, alignment, number format, exact style copying, and an
explicit `apply_to_blank_cells` switch. Formatting never changes a cell value
or formula and preserves unrelated package parts byte-for-byte.

Surgical helpers change only the targeted XML parts, report the exact package
part delta, and verify that macro payloads remain byte-identical. They never
execute macros. Rich native operations—tables, charts, validation, layouts,
fields, forms, annotations, and drawings—remain available through raw
Document Builder JavaScript when a surgical operation is not expressive
enough.

## Finalizer policy

The runtime keeps LibreOffice, Pandoc, Python Office libraries, direct XML,
and the other utilities available for inspection, extraction, planning, and
intermediate work. Availability does not make them approved final serializers.

- New OOXML deliverables are finalized by neoHarness Office.
- Existing OOXML revisions are finalized by neoHarness Office or by a bounded
  `nh-document ooxml` mutation with exact package-preservation evidence.
- PDF page, OCR, render, and image operations use their named domain finalizer.
- Reopening bytes from a lossy serializer does not convert those bytes into a
  preservation-faithful revision.

The canonical policy is installed at
`/opt/neoharness-office/share/runtime/quality-policy.v1.json`. Approved
operations emit artifact-bound provenance containing its version and SHA-256
digest. The publication boundary must verify server-observed provenance and
the exact output hash; selected-agent-authored JSON is not authority.

Disposable utility images enable `nh-office-finalizerd.service` and place the
attested `nh-office` and `nh-document` clients first on the sandbox user's
`PATH`. The root-owned broker runs the exact packaged finalizer as the calling
unprivileged UID, records successful artifact provenance beneath its private
runtime directory, and later qualifies only unchanged output bytes. The
gateway queries that broker through `nh-artifact-qualify`; an ordinary JSON
file in `/workspace` can never substitute for the broker's observation. The
direct engine entry points remain in the package for build and recovery, but
bytes produced by bypassing the broker have no publication authority.

The gateway seals every canonical `/workspace/input` file with
`nh-input-attest` immediately after its streamed hash and byte count match.
Document finalizers must declare each Office/PDF source through their normal
arguments. The broker accepts a declared source only when it is still the
sealed gateway input or an unchanged output of an earlier approved finalizer.
An Office/PDF file produced in `/workspace/work` by LibreOffice, Pandoc, or a
generic serializer cannot be reopened merely to launder it into qualified
publication bytes.

Images enter the same workspace unchanged and can be inspected or transformed
without weakening the original-file boundary:

```sh
nh-document image crop /workspace/input/photo.heic \
  /workspace/work/cropped.png --left 20 --top 20 --width 1200 --height 800
nh-document image fit /workspace/work/cropped.png \
  /workspace/output/for-document.png --width 1600 --height 900 --mode contain
```

The runtime includes libvips, ImageMagick, Pillow, EXIF, SVG, HEIF, WebP, and
camera-RAW adapters plus common image optimization tools. These helpers are
conveniences, not capability gates; selected-agent code may invoke the native
utilities directly.

Images, PDFs, and Office files share the same workspace and can participate in
one cross-format operation. Rendering, OCR, and PDF page composition use exact
input and output paths:

```sh
nh-document render pdf /workspace/input/source.xlsx \
  /workspace/output/source.pdf
nh-document render compare /workspace/input/before.docx \
  /workspace/output/after.docx /workspace/output/before-after.png
nh-document ocr /workspace/input/scanned.pdf \
  /workspace/output/searchable.pdf
nh-document pdf merge /workspace/input/cover.pdf \
  /workspace/output/source.pdf --output /workspace/output/combined.pdf
```

Multiple inputs and independently declared outputs can be processed inside the
same utility VM. Independent `nh-office` invocations may also run concurrently;
the caller remains responsible for giving each invocation distinct output and
manifest paths. This enables workflows such as extracting an image from a PDF,
placing it into a Word file alongside a table read from a spreadsheet, and
publishing both the editable document and a PDF without requiring a second
document authority system.

Font and theme assets are generated at build time from the pinned
source-controlled `core-fonts` tree. Host and user fonts are not indexed. This
keeps viewer and utility-runtime layout deterministic and uses the included
Liberation families for common Microsoft-font substitution without modifying
neoHarness Sans.
