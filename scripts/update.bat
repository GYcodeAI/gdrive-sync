@echo off
chcp 65001 >nul
REM gdrive-sync 업데이트 (더블클릭 실행용)
REM pip 으로 GitHub 저장소의 최신 버전을 설치합니다.
REM 비공개 저장소인 경우 GitHub 로그인 창이 뜰 수 있습니다 (최초 1회).

echo.
echo  gdrive-sync 업데이트를 시작합니다...
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    py -m pip install --upgrade git+https://github.com/GYcodeAI/gdrive-sync.git
) else (
    python -m pip install --upgrade git+https://github.com/GYcodeAI/gdrive-sync.git
)

if %errorlevel%==0 (
    echo.
    echo  업데이트 완료. 실행 중인 gdrive-sync 가 있다면 재시작하세요.
) else (
    echo.
    echo  업데이트 실패. 인터넷 연결과 GitHub 접근 권한을 확인하세요.
)
echo.
pause
