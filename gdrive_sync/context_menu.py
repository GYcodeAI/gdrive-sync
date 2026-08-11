"""Windows 탐색기 우클릭 메뉴에 'gdrive-sync GUI 실행' 항목 등록/제거.

HKCU\\Software\\Classes 아래에만 기록하므로 관리자 권한이 필요 없다.

등록 위치:
- Directory\\Background\\shell — 탐색기에서 폴더 빈 공간 우클릭
- DesktopBackground\\shell     — 바탕화면 우클릭

실행 명령은 pythonw.exe(콘솔 창 없음)로 `-m gdrive_sync gui`를 호출한다.
launch-gui.vbs 같은 외부 파일에 의존하지 않으므로 저장소 위치가
바뀌어도 재등록만 하면 된다.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import winreg
except ImportError:  # Windows 외 플랫폼
    winreg = None  # type: ignore[assignment]

MENU_TEXT = "gdrive-sync GUI 실행"
KEY_NAME = "gdrive-sync-gui"

# HKCU 기준 상대 경로 (끝에 \\{KEY_NAME}이 붙음)
_SHELL_BASES = (
    r"Software\Classes\Directory\Background\shell",   # 폴더 빈 공간
    r"Software\Classes\DesktopBackground\shell",      # 바탕화면
)


def is_supported() -> bool:
    """Windows + winreg 사용 가능 여부."""
    return winreg is not None


def pythonw_executable() -> str:
    """콘솔 창이 뜨지 않는 pythonw.exe 경로 (없으면 python.exe)."""
    exe = Path(sys.executable).resolve()
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def launch_command() -> str:
    """우클릭 메뉴가 실행할 명령 문자열."""
    return f'"{pythonw_executable()}" -m gdrive_sync gui'


def install() -> list[str]:
    """우클릭 메뉴 항목 등록. 기록한 레지스트리 경로 목록 반환."""
    _ensure_supported()
    cmd = launch_command()
    icon = pythonw_executable()
    written: list[str] = []
    for base in _SHELL_BASES:
        key_path = rf"{base}\{KEY_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, MENU_TEXT)
            winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, icon)
        with winreg.CreateKey(
            winreg.HKEY_CURRENT_USER, rf"{key_path}\command"
        ) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, cmd)
        written.append(rf"HKCU\{key_path}")
        logger.debug("우클릭 메뉴 등록: %s → %s", key_path, cmd)
    return written


def remove() -> list[str]:
    """우클릭 메뉴 항목 제거. 실제로 지운 레지스트리 경로 목록 반환."""
    _ensure_supported()
    removed: list[str] = []
    for base in _SHELL_BASES:
        key_path = rf"{base}\{KEY_NAME}"
        found = False
        # 하위 키(command)부터 지워야 부모 삭제가 성공한다
        for sub in (rf"{key_path}\command", key_path):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
                found = True
            except FileNotFoundError:
                pass
        if found:
            removed.append(rf"HKCU\{key_path}")
            logger.debug("우클릭 메뉴 제거: %s", key_path)
    return removed


def status() -> dict:
    """레지스트리 경로별 등록 여부 {경로: bool}."""
    _ensure_supported()
    result = {}
    for base in _SHELL_BASES:
        key_path = rf"{base}\{KEY_NAME}"
        try:
            winreg.CloseKey(
                winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path)
            )
            result[rf"HKCU\{key_path}"] = True
        except FileNotFoundError:
            result[rf"HKCU\{key_path}"] = False
    return result


def _ensure_supported() -> None:
    if not is_supported():
        raise RuntimeError("우클릭 메뉴 등록은 Windows에서만 지원됩니다.")
