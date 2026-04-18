Dim fso, projDir, oldLog
Set fso = CreateObject("Scripting.FileSystemObject")
projDir = fso.GetParentFolderName(WScript.ScriptFullName)
oldLog = projDir & "\server.log"
If fso.FileExists(oldLog) Then fso.MoveFile oldLog, projDir & "\server.prev.log"
CreateObject("WScript.Shell").Run "cmd /c cd /d """ & projDir & """ && python -u spotify_server.py > server.log 2>&1", 0, False
