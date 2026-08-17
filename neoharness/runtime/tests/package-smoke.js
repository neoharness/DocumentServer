builder.CreateFile("docx");
var document = Api.GetDocument();
document.GetElement(0).AddText("NHO_HEADLESS_RUNTIME_SMOKE");
builder.SaveFile("docx", "/workspace/output/package-smoke.docx");
builder.CloseFile();
