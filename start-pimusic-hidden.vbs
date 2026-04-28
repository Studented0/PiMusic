Dim fso, shell, projDir, oldLog, prevLog
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

projDir = fso.GetParentFolderName(WScript.ScriptFullName)
oldLog = projDir & "\server.log"
prevLog = projDir & "\server.prev.log"

' Kill any existing python server process first so server.log is released
shell.Run "cmd /c taskkill /F /IM python.exe /T", 0, True

' Give it a moment to fully release file handles
WScript.Sleep 500

' Now safely rotate the log
If fso.FileExists(oldLog) Then
    If fso.FileExists(prevLog) Then fso.DeleteFile prevLog
    fso.MoveFile oldLog, prevLog
End If

' Start the server hidden
shell.Run "cmd /c cd /d """ & projDir & """ && python -u spotify_server.py > server.log 2>&1", 0, False