Option Explicit
Dim WshShell, FSO, currentDir, scriptPath, pythonPath, pythonwPath, exitCode
Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")
currentDir = FSO.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = currentDir
scriptPath = currentDir & "\codex_auto_retry_v2.py"
pythonPath = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\python.exe"
pythonwPath = WshShell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python312\pythonw.exe"
If Not FSO.FileExists(pythonPath) Then pythonPath = "python"
If Not FSO.FileExists(pythonwPath) Then pythonwPath = pythonPath
exitCode = WshShell.Run("""" & pythonPath & """ """ & scriptPath & """ --check", 0, True)
If exitCode <> 0 Then
    WScript.Echo "Preflight failed. Run start.bat to see diagnostics."
    WScript.Quit 1
End If
WshShell.Run """" & pythonwPath & """ -u """ & scriptPath & """", 0, False
WScript.Echo "V2 background launch requested." & vbCrLf & "Status and startup errors: " & currentDir & "\logs\auto-retry.log" & vbCrLf & "Duplicate instances are not started. Use stop.bat to stop this copy."
