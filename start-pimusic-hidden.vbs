Dim fso, projDir, oldLog
Set fso = CreateObject("Scripting.FileSystemObject")
projDir = fso.GetParentFolderName(WScript.ScriptFullName)
oldLog = projDir & "\server.log"
If fso.FileExists(oldLog) Then
    Dim prevLog
    prevLog = projDir & "\server.prev.log"
    If fso.FileExists(prevLog) Then fso.DeleteFile prevLog
    fso.MoveFile oldLog, prevLog
End If
CreateObject("WScript.Shell").Run "cmd /c cd /d """ & projDir & """ && python -u spotify_server.py > server.log 2>&1", 0, False
