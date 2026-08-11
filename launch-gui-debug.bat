@echo off
REM gdrive-sync GUI 디버그 런처
REM launch-gui.bat가 동작하지 않을 때, 무슨 일이 벌어지는지 확인하는 용도.
REM
REM 이 파일을 더블클릭하면:
REM   1. 콘솔 창이 뜸 (닫지 말 것)
REM   2. python.exe로 GUI를 실행 (pythonw.exe 아님 — 모든 출력 보임)
REM   3. GUI가 뜨든 에러든 모든 메시지가 콘솔에 표시됨
REM   4. 종료 후 pause — 사용자가 내용을 보고 창 닫기

chcp 65001 > nul 2>&1
cd /d "%~dp0"

echo.
echo ========================================================
echo   gdrive-sync GUI Debug Launcher
echo ========================================================
echo 현재 디렉토리: %CD%
echo.
echo [1] Python 버전 확인 중...
"C:\Python314\python.exe" --version
if errorlevel 1 (
    echo.
    echo X  C:\Python314\python.exe 가 없거나 실행 실패
    echo     → Python 3.14가 다른 경로에 설치되어 있을 수 있습니다.
    goto :end
)

echo.
echo [2] gdrive_sync 패키지 로드 확인 중...
"C:\Python314\python.exe" -c "import gdrive_sync; print('버전:', gdrive_sync.__version__)"
if errorlevel 1 (
    echo.
    echo X  gdrive_sync 패키지를 import 할 수 없습니다.
    echo     → 다음 명령으로 재설치 필요:
    echo        py -3.14 -m pip install -e .
    goto :end
)

echo.
echo [3] Tkinter 확인 중...
"C:\Python314\python.exe" -c "import tkinter; print('Tkinter:', tkinter.TkVersion)"
if errorlevel 1 (
    echo.
    echo X  Tkinter 를 import 할 수 없습니다.
    echo     → Python 재설치 시 "tcl/tk and IDLE" 옵션 체크 필요
    goto :end
)

echo.
echo [4] GUI 실행 중... (창이 뜨면 정상)
echo     GUI를 닫으면 이 콘솔도 닫을 수 있습니다.
echo.
"C:\Python314\python.exe" -m gdrive_sync gui

echo.
echo ========================================================
echo GUI 종료됨. 종료 코드: %ERRORLEVEL%
echo ========================================================

:end
echo.
pause
