// builder-host.js — neoHarness Office Document Builder HOST COMMAND reference.
//
// This file documents the builder.* native command surface implemented by the
// installed docbuilder engine (core/DesktopEditor/doctrenderer). It is a
// documentation file, not executable JavaScript. The editor object APIs
// (Api.GetDocument, Api.GetActiveSheet, paragraphs, ranges, slides, forms)
// are documented separately in word-apiBuilder.js, cell-apiBuilder.js,
// slide-apiBuilder.js, and pdf-apiBuilder.js.
//
// ============================================================================
// HOW THE ENGINE PARSES A SCRIPT (chunking semantics)
// ============================================================================
// The engine reads the script line by line. A line whose whitespace-trimmed
// start is "builder." is executed as a NATIVE COMMAND, not as JavaScript.
// Every other line accumulates into a JavaScript chunk that executes when the
// next builder.* native command line arrives (or at end of script).
//
// Consequences you must design around:
// - builder is NOT a JavaScript global. "typeof builder" or builder.X inside
//   a larger JavaScript expression throws ReferenceError: builder is not
//   defined. Only a line that BEGINS with builder. is a native command.
// - A builder.*() line inside an if/try/loop block SPLITS the surrounding
//   JavaScript into separate broken fragments and produces
//   SyntaxError: Unexpected end of input. Keep every builder.*() call at the
//   top level of the script, never inside a JavaScript block.
// - JavaScript queued before the first CreateFile/OpenFile executes only
//   after that file context is created. If the open itself fails, the queued
//   JavaScript (including console.log output) is silently discarded.
//
// ============================================================================
// NATIVE COMMAND ARGUMENTS AND THE jsValue DYNAMIC-PATH BRIDGE
// ============================================================================
// The native command parser extracts ONLY quote-delimited strings from the
// argument list. It never evaluates JavaScript inside a builder.*() call.
// A bare expression such as builder.SaveFile("docx", Argument.output) sends
// an EMPTY path to the engine and fails with:
//   Empty sFileFrom or sFileTo
//   error: : save file error (88)
//
// To pass a JavaScript value (for example a dynamic output path) into a
// native command, assign it to a named variable and pass the LITERAL bridge
// string "jsValue(variableName)". The engine detects the jsValue( prefix in
// a quoted parameter and resolves the named JavaScript variable itself:
//
//   var inputPath = Argument["input"];
//   builder.OpenFile("jsValue(inputPath)");
//   var outputPath = Argument["output"];
//   builder.SaveFile("docx", "jsValue(outputPath)");
//
// "jsValue(outputPath)" is passed through exactly as written — keep the
// quotes; do not replace the text with the variable's value. The bridge
// works in every builder.* native command parameter. Static literal paths
// in quotes (for example "/workspace/output/report.docx") also work.
//
// ============================================================================
// builder.CreateFile(type) — create a new empty document (create word file,
// create document, create spreadsheet, create workbook, create presentation,
// new file from scratch)
// ============================================================================
// builder.CreateFile("docx");   // new Word document (WordprocessingML)
// builder.CreateFile("docxf");  // new form document (docx class)
// builder.CreateFile("oform");  // new fillable form (docx class)
// builder.CreateFile("form");   // new form (docx class)
// builder.CreateFile("pdf");    // authored through the Word document surface
// builder.CreateFile("pptx");   // new presentation (PresentationML)
// builder.CreateFile("xlsx");   // new workbook (SpreadsheetML)
// builder.CreateFile("vsdx");   // new drawing (Visio)
// After CreateFile, use the matching editor API: Api.GetDocument() for the
// docx class, Api.GetActiveSheet()/Api.GetSheets() for xlsx,
// Api.GetPresentation() for pptx.
// To produce a PDF from scratch: builder.CreateFile("docx"), author content
// with the Word API, then builder.SaveFile("pdf", "jsValue(outputPath)").
//
// ============================================================================
// builder.OpenFile(path) — open an existing document (open file, load
// document, read existing file, revise document)
// ============================================================================
// var inputPath = Argument["input"];
// builder.OpenFile("jsValue(inputPath)");
// The format is detected from the opened bytes/extension. A failed open is
// SILENT: queued JavaScript is discarded and the run exits nonzero with no
// diagnostics, so verify the input path exists and the bridge is literal.
//
// ============================================================================
// builder.SaveFile(format, path) — save the open document (save document,
// save file, export, write output, convert to pdf, produce docx)
// ============================================================================
// var outputPath = Argument["output"];
// builder.SaveFile("docx", "jsValue(outputPath)");
// builder.SaveFile("xlsx", "jsValue(outputPath)");
// builder.SaveFile("pptx", "jsValue(outputPath)");
// builder.SaveFile("pdf",  "jsValue(outputPath)");
// The first parameter is the target format token (resolved by extension).
// Cross-format saves are supported where the engine supports the conversion
// (for example an open docx saved as "pdf").
//
// ============================================================================
// builder.CloseFile() — close the open document context (close document,
// finish, end)
// ============================================================================
// builder.CloseFile();
// Call once after the final SaveFile. Scripts may open another file after
// closing, but one open-edit-save-close sequence per run is the reliable
// pattern.
//
// ============================================================================
// builder.SetTmpFolder(path) — set the engine scratch folder (temporary
// directory, tmp folder)
// ============================================================================
// builder.SetTmpFolder("/workspace/work");
// Optional; the launcher already runs with a safe default.
//
// ============================================================================
// builder.WriteData(path, value, isAppend) — append text to a file (write
// text file, log output, export text)
// ============================================================================
// builder.WriteData("/workspace/output/log.txt", "jsValue(textVariable)", true);
// Writes/appends a string value to a file. Prefer console.log for run
// diagnostics; the launcher captures stdout into the manifest.
//
// ============================================================================
// MINIMAL COMPLETE EXAMPLES
// ============================================================================
// Create a Word document from scratch and save it:
//   builder.CreateFile("docx");
//   var document = Api.GetDocument();
//   document.GetElement(0).AddText("Hello");
//   var outputPath = Argument["output"];
//   builder.SaveFile("docx", "jsValue(outputPath)");
//   builder.CloseFile();
//
// Revise an existing workbook:
//   var inputPath = Argument["input"];
//   builder.OpenFile("jsValue(inputPath)");
//   Api.GetActiveSheet().GetRange("A1").SetValue("Updated");
//   var outputPath = Argument["output"];
//   builder.SaveFile("xlsx", "jsValue(outputPath)");
//   builder.CloseFile();
//
//
// ============================================================================
// API.CREATEIMAGE
// ============================================================================
// Api.CreateImage(source, widthEmu, heightEmu) returns a raster drawing.
// It does not insert the drawing by itself; attach the returned object to a
// paragraph (or another supported container) with AddDrawing.
// source must be a data URI: "data:image/png;base64,..." or
// "data:image/jpeg;base64,...".  File paths and file:// URIs silently
// produce an empty media directory — never use them.
// Use only when adding genuinely new images; existing source assets carry
// forward naturally when you open and modify the supplied DOCX rather than
// authoring from a blank document.
//
//   var logoUri = "data:image/png;base64," + Argument["argument"]["logo_b64"];
//   var image = Api.CreateImage(logoUri, 3000000, 900000);  // EMU
//   Api.GetDocument().GetCurrentParagraph().AddDrawing(image);
//
// ============================================================================
// PDF EXPORT AFTER ADDING A NEW IMAGE
// ============================================================================
// Existing images in a DOCX opened with builder.OpenFile carry through a
// direct builder.SaveFile("pdf"). If the current script adds a NEW in-memory
// image with Api.CreateImage, direct PDF save fails with a deterministic
// engine error. Save the DOCX first, then reopen it before exporting to PDF:
//
//   var docxPath = Argument["outputs"][0];
//   builder.SaveFile("docx", "jsValue(docxPath)");
//   builder.CloseFile();
//   var reopenPath = Argument["outputs"][0];  // re-declare after CloseFile
//   builder.OpenFile("jsValue(reopenPath)");
//   var pdfPath = Argument["outputs"][1];
//   builder.SaveFile("pdf", "jsValue(pdfPath)");
//   builder.CloseFile();
// Declare two --output arguments only for this fallback.
//
// The launcher (nh-office run) supplies Argument as parsed JSON with keys
// input, inputs, output, outputs, and argument (the caller's own JSON).
