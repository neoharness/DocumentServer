var inputPath0 = Argument["inputs"][0];
var outputPath0 = Argument["outputs"][0];

builder.OpenFile("jsValue(inputPath0)");
var oDocument = Api.GetDocument();
oDocument.GetElement(0).AddText(" NHO_RUNTIME_MULTI_DOCX_EDITED");
builder.SaveFile("docx", "jsValue(outputPath0)");
builder.CloseFile();

// Closing a document resets the native JavaScript context. Reacquire values
// from the re-initialized Argument object instead of carrying variables across
// the document boundary.
var inputPath1 = Argument["inputs"][1];
var outputPath1 = Argument["outputs"][1];
builder.OpenFile("jsValue(inputPath1)");
var oSheet = Api.GetActiveSheet();
oSheet.GetRange("A3").SetValue("NHO_RUNTIME_MULTI_XLSX_EDITED");
builder.SaveFile("xlsx", "jsValue(outputPath1)");
builder.CloseFile();
