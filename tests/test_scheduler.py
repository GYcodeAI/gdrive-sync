"""스케줄러 테스트: 파싱 + OS별 명령 생성 (실제 등록은 mock)."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gdrive_sync.config import SchedulerJob
from gdrive_sync.scheduler import (
    LinuxScheduler, MacOSScheduler, ParsedSchedule, ScheduleParseError,
    WindowsScheduler, _parse_cron, _parse_cron_dow, default_python_executable,
    get_scheduler, parse_job,
)


# ──────────────────────────────────────────────────────────
# 구조화 파싱
# ──────────────────────────────────────────────────────────

def test_parse_job_daily():
    job = SchedulerJob(name="점심", type="daily", time="12:30")
    s = parse_job(job)
    assert s.kind == "daily"
    assert s.hour == 12 and s.minute == 30


def test_parse_job_weekly():
    job = SchedulerJob(
        name="회사",
        type="weekly",
        time="18:00",
        weekdays=["mon", "tue", "wed", "thu", "fri"],
    )
    s = parse_job(job)
    assert s.kind == "weekly"
    assert s.hour == 18 and s.minute == 0
    assert s.weekdays == [0, 1, 2, 3, 4]


def test_parse_job_weekly_default_weekdays():
    job = SchedulerJob(name="자동", type="weekly", time="09:00")
    s = parse_job(job)
    assert s.weekdays == [0, 1, 2, 3, 4]  # 평일 기본


def test_parse_job_hourly():
    job = SchedulerJob(name="h", type="hourly", minute=15)
    s = parse_job(job)
    assert s.kind == "hourly"
    assert s.minute == 15


def test_parse_job_interval():
    job = SchedulerJob(name="i", type="interval", interval_minutes=30)
    s = parse_job(job)
    assert s.kind == "interval"
    assert s.interval_minutes == 30


def test_parse_job_interval_missing_raises():
    job = SchedulerJob(name="i", type="interval")
    with pytest.raises(ScheduleParseError):
        parse_job(job)


def test_parse_job_unknown_type_raises():
    job = SchedulerJob(name="x", type="weird")
    with pytest.raises(ScheduleParseError):
        parse_job(job)


def test_parse_job_no_type_no_cron_raises():
    job = SchedulerJob(name="x")
    with pytest.raises(ScheduleParseError):
        parse_job(job)


def test_parse_job_bad_time_raises():
    job = SchedulerJob(name="x", type="daily", time="25:00")
    with pytest.raises(ScheduleParseError):
        parse_job(job)


# ──────────────────────────────────────────────────────────
# cron 파서
# ──────────────────────────────────────────────────────────

def test_parse_cron_daily():
    s = _parse_cron("30 12 * * *")
    assert s.kind == "daily"
    assert s.hour == 12 and s.minute == 30


def test_parse_cron_hourly():
    s = _parse_cron("15 * * * *")
    assert s.kind == "hourly"
    assert s.minute == 15


def test_parse_cron_interval():
    s = _parse_cron("*/5 * * * *")
    assert s.kind == "interval"
    assert s.interval_minutes == 5


def test_parse_cron_weekly_range():
    s = _parse_cron("0 18 * * 1-5")
    assert s.kind == "weekly"
    assert s.hour == 18 and s.minute == 0
    # cron 1-5 → 월~금 → python 0,1,2,3,4
    assert s.weekdays == [0, 1, 2, 3, 4]


def test_parse_cron_weekly_list():
    s = _parse_cron("0 9 * * 1,3,5")
    assert s.kind == "weekly"
    # 월, 수, 금
    assert s.weekdays == [0, 2, 4]


def test_parse_cron_sunday_variants():
    # cron 0 = 일 → python 6
    assert _parse_cron_dow("0") == [6]
    # cron 7 = 일 → python 6
    assert _parse_cron_dow("7") == [6]


def test_parse_cron_invalid_raises():
    with pytest.raises(ScheduleParseError):
        _parse_cron("not a cron")
    with pytest.raises(ScheduleParseError):
        _parse_cron("0 12 1 * *")  # DOM != *


def test_parse_job_cron_takes_priority():
    job = SchedulerJob(name="x", cron="30 9 * * *", type="hourly")
    s = parse_job(job)
    assert s.kind == "daily"
    assert s.hour == 9 and s.minute == 30


# ──────────────────────────────────────────────────────────
# WindowsScheduler 명령 생성
# ──────────────────────────────────────────────────────────

def test_windows_scheduler_daily_command():
    s = WindowsScheduler()
    captured = {}
    def fake_run(args):
        captured["args"] = args
    with patch("gdrive_sync.scheduler._run_and_raise", side_effect=fake_run):
        s.register(
            SchedulerJob(name="점심", type="daily", time="12:00"),
            python_exe="C:/Python314/python.exe",
        )
    args = captured["args"]
    assert "/sc" in args and "DAILY" in args
    assert "/st" in args
    assert args[args.index("/st") + 1] == "12:00"
    assert args[args.index("/tn") + 1] == "gdrive-sync-점심"


def test_windows_scheduler_weekly_command():
    s = WindowsScheduler()
    captured = {}
    with patch(
        "gdrive_sync.scheduler._run_and_raise",
        side_effect=lambda args: captured.update(args=args),
    ):
        s.register(
            SchedulerJob(
                name="weekday", type="weekly", time="18:30",
                weekdays=["mon", "tue", "wed", "thu", "fri"],
            ),
            python_exe="C:/Python314/python.exe",
        )
    args = captured["args"]
    assert "WEEKLY" in args
    d_idx = args.index("/d")
    assert args[d_idx + 1] == "MON,TUE,WED,THU,FRI"


def test_windows_scheduler_interval_command():
    s = WindowsScheduler()
    captured = {}
    with patch(
        "gdrive_sync.scheduler._run_and_raise",
        side_effect=lambda args: captured.update(args=args),
    ):
        s.register(
            SchedulerJob(name="i", type="interval", interval_minutes=30),
            python_exe="python",
        )
    args = captured["args"]
    assert "MINUTE" in args
    mo_idx = args.index("/mo")
    assert args[mo_idx + 1] == "30"


# ──────────────────────────────────────────────────────────
# MacOSScheduler plist 생성
# ──────────────────────────────────────────────────────────

def test_macos_scheduler_generates_plist(tmp_path, monkeypatch):
    # home을 임시 디렉토리로
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    s = MacOSScheduler()
    with patch("gdrive_sync.scheduler._run_and_raise"), \
         patch("subprocess.call", return_value=0):
        label = s.register(
            SchedulerJob(name="lunch", type="daily", time="12:00"),
            python_exe="/usr/bin/python3",
        )

    assert label == "com.gdrive-sync.lunch"
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.gdrive-sync.lunch.plist"
    assert plist_path.exists()

    import plistlib
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    assert data["Label"] == "com.gdrive-sync.lunch"
    assert "/usr/bin/python3" in data["ProgramArguments"]
    assert "gdrive_sync" in data["ProgramArguments"]
    assert data["StartCalendarInterval"]["Hour"] == 12
    assert data["StartCalendarInterval"]["Minute"] == 0


def test_macos_scheduler_weekly_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    s = MacOSScheduler()
    with patch("gdrive_sync.scheduler._run_and_raise"), \
         patch("subprocess.call", return_value=0):
        s.register(
            SchedulerJob(
                name="weekdays", type="weekly", time="09:00",
                weekdays=["mon", "tue", "wed", "thu", "fri"],
            ),
            python_exe="/usr/bin/python3",
        )

    import plistlib
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.gdrive-sync.weekdays.plist"
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    # 5개 요일 → 5개 entries
    assert isinstance(data["StartCalendarInterval"], list)
    assert len(data["StartCalendarInterval"]) == 5


def test_macos_scheduler_interval_plist(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    s = MacOSScheduler()
    with patch("gdrive_sync.scheduler._run_and_raise"), \
         patch("subprocess.call", return_value=0):
        s.register(
            SchedulerJob(name="poll", type="interval", interval_minutes=15),
            python_exe="/usr/bin/python3",
        )

    import plistlib
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.gdrive-sync.poll.plist"
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)
    assert data["StartInterval"] == 15 * 60


# ──────────────────────────────────────────────────────────
# LinuxScheduler crontab 생성
# ──────────────────────────────────────────────────────────

def test_linux_scheduler_to_cron_daily():
    s = LinuxScheduler()
    ps = ParsedSchedule(kind="daily", hour=12, minute=30)
    assert s._to_cron(ps) == "30 12 * * *"


def test_linux_scheduler_to_cron_weekly():
    s = LinuxScheduler()
    ps = ParsedSchedule(kind="weekly", hour=9, minute=0, weekdays=[0, 1, 2, 3, 4])
    # python 0~4(월~금) → cron 1~5
    assert s._to_cron(ps) == "0 9 * * 1,2,3,4,5"


def test_linux_scheduler_to_cron_sunday_mapping():
    s = LinuxScheduler()
    ps = ParsedSchedule(kind="weekly", hour=10, minute=0, weekdays=[6])
    # python 6(일) → cron 0
    assert s._to_cron(ps) == "0 10 * * 0"


def test_linux_scheduler_to_cron_hourly():
    s = LinuxScheduler()
    ps = ParsedSchedule(kind="hourly", minute=15)
    assert s._to_cron(ps) == "15 * * * *"


def test_linux_scheduler_to_cron_interval():
    s = LinuxScheduler()
    ps = ParsedSchedule(kind="interval", interval_minutes=5)
    assert s._to_cron(ps) == "*/5 * * * *"


def test_linux_scheduler_register_and_list(monkeypatch):
    s = LinuxScheduler()

    # 가짜 crontab 저장소
    storage = {"content": ""}

    def fake_read():
        return storage["content"].splitlines()

    def fake_write(text):
        storage["content"] = text

    monkeypatch.setattr(s, "_read_crontab", fake_read)
    monkeypatch.setattr(s, "_write_crontab", fake_write)

    s.register(
        SchedulerJob(name="morning", type="daily", time="09:00"),
        python_exe="/usr/bin/python3",
    )
    assert "gdrive-sync-morning" in s.list_jobs()
    assert "# job: morning" in storage["content"]
    assert "0 9 * * *" in storage["content"]

    # 제거
    ok = s.unregister("morning")
    assert ok
    assert s.list_jobs() == []


# ──────────────────────────────────────────────────────────
# 팩토리 / Python 실행 경로
# ──────────────────────────────────────────────────────────

def test_get_scheduler_returns_correct_type():
    s = get_scheduler()
    if sys.platform == "win32":
        assert isinstance(s, WindowsScheduler)
    elif sys.platform == "darwin":
        assert isinstance(s, MacOSScheduler)
    else:
        assert isinstance(s, LinuxScheduler)


def test_default_python_executable_is_absolute():
    p = default_python_executable()
    assert Path(p).is_absolute()
