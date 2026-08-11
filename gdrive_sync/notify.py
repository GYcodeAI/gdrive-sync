"""크로스플랫폼 토스트/데스크톱 알림.

의존성 없이 OS 네이티브 기능만 사용:
- Windows: PowerShell의 .NET BurntToast 또는 WinRT 토스트 알림
- macOS:   osascript (AppleScript display notification)
- Linux:   notify-send (libnotify)

모두 실패 시 fallback: Tkinter messagebox (GUI가 살아있을 때만)
"""

from __future__ import annotations

import logging
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)

APP_NAME = "gdrive-sync"


def notify(
    title: str,
    message: str,
    icon: str = "info",         # "info" | "warning" | "error"
    timeout_ms: int = 8000,
) -> bool:
    """데스크톱 토스트 알림 표시.

    반환: 표시 성공 여부. 실패해도 앱 동작에 영향 없음.
    """
    try:
        if sys.platform == "win32":
            return _notify_windows(title, message, timeout_ms)
        elif sys.platform == "darwin":
            return _notify_macos(title, message)
        else:
            return _notify_linux(title, message, icon, timeout_ms)
    except Exception as e:
        log.debug(f"토스트 알림 실패 (무시): {e}")
        return False


# ──────────────────────────────────────────────────────────
# Windows: PowerShell 토스트
# ──────────────────────────────────────────────────────────

def _notify_windows(title: str, message: str, timeout_ms: int) -> bool:
    """Windows 10/11 토스트 알림 (PowerShell + .NET)."""
    # 방법 1: Windows 10+ AppUserModelId 토스트 (가장 안정적)
    ps_script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

$template = @"
<toast>
  <visual>
    <binding template="ToastGeneric">
      <text>{_escape_xml(title)}</text>
      <text>{_escape_xml(message)}</text>
    </binding>
  </visual>
</toast>
"@

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($template)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("{APP_NAME}").Show($toast)
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if result.returncode == 0:
            return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # 방법 2: 간단한 BalloonTip fallback
    return _notify_windows_balloon(title, message, timeout_ms)


def _notify_windows_balloon(title: str, message: str, timeout_ms: int) -> bool:
    """시스템 트레이 풍선 알림 (Windows 7+ 호환)."""
    ps_balloon = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = '{_escape_ps(title)}'
$n.BalloonTipText = '{_escape_ps(message)}'
$n.Visible = $true
$n.ShowBalloonTip({timeout_ms})
Start-Sleep -Milliseconds {min(timeout_ms + 500, 10000)}
$n.Dispose()
"""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_balloon],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return result.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────
# macOS: AppleScript
# ──────────────────────────────────────────────────────────

def _notify_macos(title: str, message: str) -> bool:
    """macOS 알림 센터 (osascript)."""
    script = (
        f'display notification "{_escape_applescript(message)}" '
        f'with title "{_escape_applescript(title)}" '
        f'subtitle "{APP_NAME}"'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────
# Linux: notify-send
# ──────────────────────────────────────────────────────────

def _notify_linux(title: str, message: str, icon: str, timeout_ms: int) -> bool:
    """Linux 데스크톱 알림 (notify-send / libnotify)."""
    icon_map = {"info": "dialog-information", "warning": "dialog-warning", "error": "dialog-error"}
    try:
        result = subprocess.run(
            [
                "notify-send",
                "--app-name", APP_NAME,
                "--icon", icon_map.get(icon, "dialog-information"),
                "--expire-time", str(timeout_ms),
                title,
                message,
            ],
            capture_output=True, text=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# ──────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────

def _escape_xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def _escape_ps(s: str) -> str:
    return s.replace("'", "''").replace('"', '`"')

def _escape_applescript(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ──────────────────────────────────────────────────────────
# 동기화 완료 알림 헬퍼
# ──────────────────────────────────────────────────────────

def notify_sync_complete(
    uploaded: int,
    downloaded: int,
    errors: int,
    elapsed: float,
) -> None:
    """동기화 완료 후 토스트 표시."""
    if errors:
        icon = "warning"
        title = f"⚠ 동기화 완료 (오류 {errors}개)"
    else:
        icon = "info"
        title = "✓ 동기화 완료"

    parts = []
    if uploaded:
        parts.append(f"↑ 업로드 {uploaded}개")
    if downloaded:
        parts.append(f"↓ 다운로드 {downloaded}개")
    if not parts:
        parts.append("변경 없음")
    parts.append(f"({elapsed:.0f}초)")
    message = "  ".join(parts)

    notify(title, message, icon=icon)
