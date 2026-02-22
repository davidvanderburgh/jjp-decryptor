' JJP Asset Decryptor launcher for the installed (bundled Python) layout.
' Launches the app with no console window, using the bundled Python runtime
' and setting TCL/TK environment variables for tkinter.

Set fso = CreateObject("Scripting.FileSystemObject")
Set WshShell = CreateObject("WScript.Shell")

' Application directory (where this script lives)
appDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = appDir

' Bundled Python directory
pythonDir = appDir & "\python"

' Set TCL/TK environment so tkinter can find its runtime files
Set env = WshShell.Environment("Process")
env("TCL_LIBRARY") = pythonDir & "\tcl\tcl8.6"
env("TK_LIBRARY") = pythonDir & "\tcl\tk8.6"

' Launch with bundled pythonw.exe (no console window)
WshShell.Run """" & pythonDir & "\pythonw.exe"" """ & appDir & "\JJP Asset Decryptor.pyw""", 0, False
