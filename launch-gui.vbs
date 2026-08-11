' gdrive-sync GUI 런처 (Windows, VBScript)
'
' 이 파일을 더블클릭하면 어떤 콘솔 창도 뜨지 않고 GUI만 조용히 실행됩니다.
' launch-gui.bat 가 안 되는 환경(보안 SW 등)에서 대안으로 사용하세요.
'
' 동작:
'   1. 이 스크립트가 있는 폴더로 이동
'   2. pythonw.exe로 gdrive_sync 모듈의 gui 명령을 백그라운드 실행
'   3. 창 없이(0), 대기하지 않음(False)

Option Explicit

Dim oShell, oFSO, sDir, sPython, sCmd

Set oShell = CreateObject("WScript.Shell")
Set oFSO = CreateObject("Scripting.FileSystemObject")

' 이 스크립트 파일이 있는 폴더
sDir = oFSO.GetParentFolderName(WScript.ScriptFullName)
oShell.CurrentDirectory = sDir

' Python 실행 경로 결정 (있는 것부터)
If oFSO.FileExists("C:\Python314\pythonw.exe") Then
    sPython = """C:\Python314\pythonw.exe"""
ElseIf oFSO.FileExists("C:\Python314\python.exe") Then
    sPython = """C:\Python314\python.exe"""
Else
    sPython = "pyw -3.14"
End If

sCmd = sPython & " -m gdrive_sync gui"

' 0 = 창 숨김, False = 완료 대기 안 함
On Error Resume Next
oShell.Run sCmd, 0, False
If Err.Number <> 0 Then
    MsgBox "gdrive-sync GUI 실행 실패:" & vbCrLf & vbCrLf & _
           "명령: " & sCmd & vbCrLf & _
           "경로: " & sDir & vbCrLf & vbCrLf & _
           "오류: " & Err.Description, _
           vbCritical, "gdrive-sync 오류"
End If
