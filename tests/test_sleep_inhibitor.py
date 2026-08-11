"""SleepInhibitor 단위 테스트.

플랫폼별 명령어 빌드 + 시작/종료 멱등성 검증.
실제 자식 프로세스를 띄우면 OS에 따라 sudo 등 필요할 수 있어
subprocess.Popen은 mock으로 대체.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from gdrive_sync.sleep_inhibitor import (
    SleepInhibitor,
    mac_lid_is_closed,
    amphetamine_start_session,
    amphetamine_end_session,
    amphetamine_is_active,
    terminate_launcher_applet,
)


def test_build_command_macos():
    inh = SleepInhibitor()
    with patch("sys.platform", "darwin"):
        cmd = inh._build_command()
    # -i idle / -m 디스크 / -s 시스템 + -w <pid>(부모와 수명 결합 → 고아 방지)
    assert cmd == ["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())]
    # -d(디스플레이), -u(사용자 활동)는 의도적으로 제외 → 화면 잠금/화면보호기 허용
    assert "-d" not in cmd and "-u" not in cmd and "-dimsu" not in cmd


def test_build_command_linux():
    inh = SleepInhibitor()
    with patch("sys.platform", "linux"):
        cmd = inh._build_command()
    assert cmd is not None
    assert cmd[0] == "systemd-inhibit"
    assert "sleep" in cmd
    assert "infinity" in cmd


def test_build_command_windows_returns_none():
    """Windows는 ctypes 경로를 사용하므로 _build_command는 여전히 None."""
    inh = SleepInhibitor()
    with patch("sys.platform", "win32"):
        cmd = inh._build_command()
    assert cmd is None


def test_start_returns_false_on_unsupported_os():
    """진짜 미지원 OS(예: freebsd)에서 start()는 False 반환, 예외 안 던짐."""
    inh = SleepInhibitor()
    # win32/darwin/linux가 아닌 플랫폼으로 가장 + _build_command도 None 반환
    with patch("gdrive_sync.sleep_inhibitor.sys.platform", "freebsd"):
        with patch.object(inh, "_build_command", return_value=None):
            assert inh.start() is False
    assert inh.is_active() is False


def test_start_windows_uses_ctypes():
    """Windows 분기: SetThreadExecutionState 호출 시 start() True 반환."""
    inh = SleepInhibitor()
    with patch("gdrive_sync.sleep_inhibitor.sys.platform", "win32"):
        with patch.object(SleepInhibitor, "_win_set_state", return_value=True) as m:
            assert inh.start() is True
            assert inh.is_active() is True
            inh.stop()
            assert inh.is_active() is False
            # start() 1회 + stop() 1회 = 2번 호출
            assert m.call_count == 2


def test_start_windows_fails_when_api_returns_zero():
    """Windows에서 SetThreadExecutionState가 0(에러) 반환 시 start False."""
    inh = SleepInhibitor()
    with patch("gdrive_sync.sleep_inhibitor.sys.platform", "win32"):
        with patch.object(SleepInhibitor, "_win_set_state", return_value=False):
            assert inh.start() is False
    assert inh.is_active() is False


def test_start_returns_false_when_command_missing():
    """caffeinate/systemd-inhibit 미설치 환경에서도 조용히 실패."""
    inh = SleepInhibitor()
    with patch("gdrive_sync.sleep_inhibitor.sys.platform", "darwin"):
        with patch.object(inh, "_build_command", return_value=["nonexistent_cmd"]):
            with patch("subprocess.Popen", side_effect=FileNotFoundError):
                assert inh.start() is False
    assert inh.is_active() is False


def test_start_idempotent():
    """이미 실행 중이면 start() 또 호출해도 새 프로세스 안 띄우고 True 반환."""
    inh = SleepInhibitor()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None  # 살아있음
    inh._proc = fake_proc

    with patch("gdrive_sync.sleep_inhibitor.sys.platform", "darwin"):
        with patch("subprocess.Popen") as mock_popen:
            result = inh.start()
    assert result is True
    mock_popen.assert_not_called()


def test_stop_idempotent():
    """안 켜진 상태에서 stop() 호출해도 안전."""
    inh = SleepInhibitor()
    inh.stop()  # should not raise
    assert inh.is_active() is False


def test_stop_terminates_running_process():
    inh = SleepInhibitor()
    fake_proc = MagicMock()
    fake_proc.poll.return_value = None
    inh._proc = fake_proc

    with patch("gdrive_sync.sleep_inhibitor.sys.platform", "darwin"):
        inh.stop()
    fake_proc.terminate.assert_called_once()
    assert inh._proc is None


def test_context_manager():
    """with 블록 사용 시 자동 start/stop."""
    inh = SleepInhibitor()
    with patch.object(inh, "start") as mock_start, \
         patch.object(inh, "stop") as mock_stop:
        with inh:
            mock_start.assert_called_once()
            mock_stop.assert_not_called()
        mock_stop.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# macOS 유틸리티 함수 테스트
# ──────────────────────────────────────────────────────────────────────────────

class TestMacLidIsClosed:
    def test_non_darwin_returns_false(self):
        with patch("sys.platform", "win32"):
            assert mac_lid_is_closed() is False

    def test_lid_closed_detected(self):
        mock_result = MagicMock()
        mock_result.stdout = "AppleClamshellState = Yes\n"
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert mac_lid_is_closed() is True

    def test_lid_open_detected(self):
        mock_result = MagicMock()
        mock_result.stdout = "AppleClamshellState = No\n"
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert mac_lid_is_closed() is False

    def test_subprocess_exception_returns_false(self):
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", side_effect=Exception("ioreg 실패")):
            assert mac_lid_is_closed() is False


class TestAmphetamineSession:
    def test_non_darwin_start_returns_false(self):
        with patch("sys.platform", "win32"):
            assert amphetamine_start_session() is False

    def test_non_darwin_end_returns_false(self):
        with patch("sys.platform", "win32"):
            assert amphetamine_end_session() is False

    def test_start_session_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert amphetamine_start_session() is True

    def test_start_session_amphetamine_not_installed(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "execution error: Amphetamine got an error"
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert amphetamine_start_session() is False

    def test_end_session_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert amphetamine_end_session() is True

    def test_end_session_no_active_session(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "no session active"
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert amphetamine_end_session() is False

    def test_start_exception_returns_false(self):
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", side_effect=Exception("osascript 오류")):
            assert amphetamine_start_session() is False

    def test_is_active_true(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "true\n"
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert amphetamine_is_active() is True

    def test_is_active_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "false\n"
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", return_value=mock_result):
            assert amphetamine_is_active() is False


class TestTerminateLauncherApplet:
    def test_non_darwin_returns_false(self):
        with patch("sys.platform", "linux"):
            assert terminate_launcher_applet() is False

    def test_darwin_pkills_only_launcher_path(self):
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run") as mock_run:
            assert terminate_launcher_applet() is True
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            # 정확히 본 프로젝트 런처 경로만 매칭해야 함 (다른 applet 오살상 방지)
            assert cmd[0] == "pkill"
            assert cmd[1] == "-f"
            assert "launch-gui.app/Contents/MacOS/applet" in cmd[2]

    def test_darwin_exception_returns_false(self):
        with patch("sys.platform", "darwin"), \
             patch("subprocess.run", side_effect=Exception("pkill 오류")):
            assert terminate_launcher_applet() is False
