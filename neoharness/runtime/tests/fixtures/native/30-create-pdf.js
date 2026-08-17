builder.CreateFile("docx");
var oDocument = Api.GetDocument();
oDocument.GetElement(0).AddText("NHO_RUNTIME_PDF_ORIGINAL");
var outputPath = Argument["output"];
builder.SaveFile("pdf", "jsValue(outputPath)");
builder.CloseFile();
