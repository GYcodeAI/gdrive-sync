#!/bin/bash
# gdrive-sync GUI 런처 (macOS)
# 이 파일을 Finder에서 더블클릭하면 GUI가 실행됩니다.
#
# 최초 1회: 실행 권한 부여 필요
#   터미널에서: chmod +x launch-gui.command
#
# 또는 Finder에서 우클릭 → 열기 → "열기" 클릭 (Gatekeeper 승인)

# 스크립트가 있는 디렉토리로 이동
cd "$(dirname "$0")"

# Tk deprecation 경고 억제 (macOS 시스템 Tk 사용 시)
export TK_SILENCE_DEPRECATION=1

# 프로젝트 venv 우선 사용 (pip install -e . 해둔 격리 환경)
# 없으면 시스템 python3로 폴백
if [ -x ".venv/bin/python" ]; then
    exec .venv/bin/python -m gdrive_sync gui
elif command -v python3 &> /dev/null; then
    exec python3 -m gdrive_sync gui
elif command -v python &> /dev/null; then
    exec python -m gdrive_sync gui
else
    osascript -e 'display alert "Python 없음" message "Python 3이 설치되어 있지 않습니다.\n\nhttps://www.python.org/downloads/macos/ 에서 설치 후 다시 시도하세요."'
    exit 1
fi
