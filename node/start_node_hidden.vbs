' Starts the PipsFarmer MT5 manager with no window, so it can't be closed by accident.
' start_node.bat restarts it by itself if it ever stops.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "cmd /c start_node.bat", 0, False
