builder.CreateFile("docx");
var oDocument = Api.GetDocument();
oDocument.GetElement(0).AddText("NHO_RUNTIME_DOCX_ORIGINAL");
var outputPath = Argument["output"];
builder.SaveFile("docx", "jsValue(outputPath)");
builder.CloseFile();
