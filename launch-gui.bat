@echo off
REM gdrive-sync GUI 런처 (Windows)
REM 이 파일을 더블클릭하면 콘솔 없이 GUI만 뜹니다.
REM
REM 만약 이 파일이 작동하지 않으면 launch-gui.vbs 를 사용하거나
REM launch-gui-debug.bat 를 실행해 오류를 확인하세요.

start "gdrive-sync" /D "%~dp0" "C:\Python314\pythonw.exe" -m gdrive_sync gui
