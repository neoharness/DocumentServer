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
SHA-256 digest. Successful sibling artifacts remain described even if another
declared output fails.

Font and theme assets are generated at build time from the pinned
source-controlled `core-fonts` tree. Host and user fonts are not indexed. This
keeps viewer and utility-runtime layout deterministic and uses the included
Liberation families for common Microsoft-font substitution without modifying
neoHarness Sans.
