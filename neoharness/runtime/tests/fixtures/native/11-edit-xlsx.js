var inputPath = Argument["input"];
var outputPath = Argument["output"];
builder.OpenFile("jsValue(inputPath)");
var oSheet = Api.GetActiveSheet();
oSheet.GetRange("A2").SetValue("NHO_RUNTIME_XLSX_EDITED");
oSheet.GetRange("B1").SetValue(23);
builder.SaveFile("xlsx", "jsValue(outputPath)");
builder.CloseFile();
