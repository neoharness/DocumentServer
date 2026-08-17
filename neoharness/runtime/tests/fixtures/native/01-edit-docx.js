var inputPath = Argument["input"];
var outputPath = Argument["output"];
builder.OpenFile("jsValue(inputPath)");
var oDocument = Api.GetDocument();
oDocument.GetElement(0).AddText(" NHO_RUNTIME_DOCX_EDITED");
builder.SaveFile("docx", "jsValue(outputPath)");
builder.CloseFile();
