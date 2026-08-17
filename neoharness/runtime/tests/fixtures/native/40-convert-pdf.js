var inputPath = Argument["input"];
var outputPath = Argument["output"];
builder.OpenFile("jsValue(inputPath)");
builder.SaveFile("pdf", "jsValue(outputPath)");
builder.CloseFile();
