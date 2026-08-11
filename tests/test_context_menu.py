"""context_menu 테스트: 실제 레지스트리는 건드리지 않고 winreg를 mock."""

from unittest.mock import MagicMock, patch

import pytest

from gdrive_sync import context_menu as cm


def _fake_winreg():
    fake = MagicMock()
    fake.HKEY_CURRENT_USER = "HKCU_HANDLE"
    fake.REG_SZ = 1
    return fake


# ──────────────────────────────────────────────────────────
# 실행 명령
# ──────────────────────────────────────────────────────────

def test_pythonw_preferred_when_exists(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    pythonw = tmp_path / "pythonw.exe"
    python.write_text("")
    pythonw.write_text("")
    monkeypatch.setattr(cm.sys, "executable", str(python))
    assert cm.pythonw_executable() == str(pythonw.resolve())


def test_fallback_to_python_when_no_pythonw(tmp_path, monkeypatch):
    python = tmp_path / "python.exe"
    python.write_text("")
    monkeypatch.setattr(cm.sys, "executable", str(python))
    assert cm.pythonw_executable() == str(python.resolve())


def test_launch_command_quotes_exe_and_targets_gui():
    with patch.object(cm, "pythonw_executable",
                      return_value=r"C:\Program Files\Py\pythonw.exe"):
        assert cm.launch_command() == \
            r'"C:\Program Files\Py\pythonw.exe" -m gdrive_sync gui'


# ──────────────────────────────────────────────────────────
# install / remove / status
# ──────────────────────────────────────────────────────────

def test_install_writes_both_locations():
    fake = _fake_winreg()
    with patch.object(cm, "winreg", fake), \
         patch.object(cm, "pythonw_executable",
                      return_value=r"C:\py\pythonw.exe"):
        written = cm.install()

    assert written == [
        r"HKCU\Software\Classes\Directory\Background\shell\gdrive-sync-gui",
        r"HKCU\Software\Classes\DesktopBackground\shell\gdrive-sync-gui",
    ]
    created = [c.args[1] for c in fake.CreateKey.call_args_list]
    assert created == [
        r"Software\Classes\Directory\Background\shell\gdrive-sync-gui",
        r"Software\Classes\Directory\Background\shell\gdrive-sync-gui\command",
        r"Software\Classes\DesktopBackground\shell\gdrive-sync-gui",
        r"Software\Classes\DesktopBackground\shell\gdrive-sync-gui\command",
    ]
    # 기본값에 메뉴 텍스트, command 기본값에 실행 명령
    values = [c.args for c in fake.SetValueEx.call_args_list]
    texts = [v[4] for v in values if v[1] == ""]
    assert cm.MENU_TEXT in texts
    assert r'"C:\py\pythonw.exe" -m gdrive_sync gui' in texts
    icons = [v[4] for v in values if v[1] == "Icon"]
    assert icons == [r"C:\py\pythonw.exe"] * 2


def test_remove_deletes_command_before_parent():
    fake = _fake_winreg()
    with patch.object(cm, "winreg", fake):
        removed = cm.remove()

    assert len(removed) == 2
    deleted = [c.args[1] for c in fake.DeleteKey.call_args_list]
    base = r"Software\Classes\Directory\Background\shell\gdrive-sync-gui"
    assert deleted.index(rf"{base}\command") < deleted.index(base)


def test_remove_skips_missing_keys():
    fake = _fake_winreg()
    fake.DeleteKey.side_effect = FileNotFoundError
    with patch.object(cm, "winreg", fake):
        assert cm.remove() == []


def test_status_reports_registered_and_missing():
    fake = _fake_winreg()
    fake.OpenKey.side_effect = [MagicMock(), FileNotFoundError]
    with patch.object(cm, "winreg", fake):
        result = cm.status()
    assert list(result.values()) == [True, False]


def test_unsupported_platform_raises():
    with patch.object(cm, "winreg", None):
        assert not cm.is_supported()
        with pytest.raises(RuntimeError):
            cm.install()
        with pytest.raises(RuntimeError):
            cm.remove()


# ──────────────────────────────────────────────────────────
# 바탕화면 바로가기 (PowerShell 은 mock)
# ──────────────────────────────────────────────────────────

def test_create_shortcut_returns_ps_output(monkeypatch):
    monkeypatch.setattr(cm, "winreg", _fake_winreg())
    captured = {}

    def fake_ps(script):
        captured["script"] = script
        return r"C:\Users\u\Desktop\gdrive-sync.lnk"

    monkeypatch.setattr(cm, "_run_shortcut_ps", fake_ps)
    with patch.object(cm, "pythonw_executable", return_value=r"C:\Py\pythonw.exe"):
        path = cm.create_desktop_shortcut()
    assert path.endswith("gdrive-sync.lnk")
    # 핵심 요소가 스크립트에 들어갔는지
    assert "-m gdrive_sync gui" in captured["script"]
    assert r"C:\Py\pythonw.exe" in captured["script"]
    assert cm.SHORTCUT_FILENAME in captured["script"]


def test_remove_shortcut_none_when_absent(monkeypatch):
    monkeypatch.setattr(cm, "winreg", _fake_winreg())
    monkeypatch.setattr(cm, "_run_shortcut_ps", lambda s: "")
    assert cm.remove_desktop_shortcut() is None


def test_remove_shortcut_returns_deleted_path(monkeypatch):
    monkeypatch.setattr(cm, "winreg", _fake_winreg())
    monkeypatch.setattr(cm, "_run_shortcut_ps",
                        lambda s: r"C:\Users\u\Desktop\gdrive-sync.lnk")
    assert cm.remove_desktop_shortcut().endswith(".lnk")


def test_shortcut_unsupported_raises(monkeypatch):
    monkeypatch.setattr(cm, "winreg", None)
    with pytest.raises(RuntimeError):
        cm.create_desktop_shortcut()
