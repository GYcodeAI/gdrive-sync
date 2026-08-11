"""크로스플랫폼 예약 작업 등록기.

프로그램이 상주하지 않고, OS의 네이티브 스케줄러에 위임:
- Windows: schtasks.exe → 작업 스케줄러
- macOS:   launchd (~/Library/LaunchAgents/*.plist + launchctl)
- Linux:   crontab (사용자 crontab에 마커 블록 삽입)

스케줄 형식:
  A) 구조화:  type=daily | weekly | hourly | interval + (time/weekdays/minute/interval_minutes)
  B) cron:    cron="0 12 * * 1-5"  (기본 패턴만, full cron은 미지원)

두 형식 모두 내부 ParsedSchedule로 정규화 후, OS별 등록기에 전달.

Python 실행 경로: sys.executable (py -3.14 하드코딩 X)
→ 기기마다 설치 경로가 달라도, 현재 실행 중인 Python을 그대로 사용.
"""

from __future__ import annotations

import getpass
import logging
import os
import platform
import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gdrive_sync.config import SchedulerJob


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 내부 정규화된 스케줄
# ──────────────────────────────────────────────────────────

@dataclass
class ParsedSchedule:
    kind: str                               # "daily" | "weekly" | "hourly" | "interval"
    hour: Optional[int] = None              # 0~23
    minute: Optional[int] = None            # 0~59
    weekdays: list[int] = field(default_factory=list)   # 0=월 ... 6=일 (Python weekday)
    interval_minutes: Optional[int] = None


_WEEKDAY_NAMES_TO_PY = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


class ScheduleParseError(ValueError):
    pass


def parse_job(job: SchedulerJob) -> ParsedSchedule:
    """SchedulerJob → ParsedSchedule.

    cron과 구조화 형식 중 하나만 채워져 있어야 함. 둘 다 있으면 cron 우선.
    """
    if job.cron:
        return _parse_cron(job.cron)

    if not job.type:
        raise ScheduleParseError(
            f"작업 '{job.name}': type 또는 cron 중 하나는 지정해야 합니다"
        )

    kind = job.type.lower()
    if kind == "daily":
        h, m = _parse_hhmm(job.time or "")
        return ParsedSchedule(kind="daily", hour=h, minute=m)

    if kind == "weekly":
        h, m = _parse_hhmm(job.time or "")
        wd = [_WEEKDAY_NAMES_TO_PY[d.lower()] for d in job.weekdays
              if d.lower() in _WEEKDAY_NAMES_TO_PY]
        if not wd:
            wd = [0, 1, 2, 3, 4]   # 기본 평일
        return ParsedSchedule(kind="weekly", hour=h, minute=m, weekdays=wd)

    if kind == "hourly":
        m = job.minute if job.minute is not None else 0
        return ParsedSchedule(kind="hourly", minute=int(m) % 60)

    if kind == "interval":
        iv = job.interval_minutes
        if not iv or iv <= 0:
            raise ScheduleParseError(
                f"작업 '{job.name}': interval type엔 interval_minutes가 필요합니다"
            )
        return ParsedSchedule(kind="interval", interval_minutes=int(iv))

    raise ScheduleParseError(
        f"작업 '{job.name}': 알 수 없는 type '{job.type}' "
        f"(사용 가능: daily, weekly, hourly, interval)"
    )


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        h, m = s.strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError
        return h, m
    except (ValueError, AttributeError):
        raise ScheduleParseError(f"잘못된 시각 형식: '{s}' (HH:MM 필요)")


# ──────────────────────────────────────────────────────────
# 기본 cron 파서
# ──────────────────────────────────────────────────────────
# 지원 패턴:
#   M H * * *          → daily at H:M
#   M H * * D          → weekly 1일 (D = 0~7)
#   M H * * D1-D2      → weekly 연속 요일
#   M H * * D1,D2,D3   → weekly 여러 요일
#   M * * * *          → hourly at minute M
#   */N * * * *        → interval N분
# 나머지는 오류

_CRON_RE = re.compile(
    r"^\s*(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)


def _parse_cron(expr: str) -> ParsedSchedule:
    m = _CRON_RE.match(expr)
    if not m:
        raise ScheduleParseError(f"cron 형식 오류: '{expr}'")
    M, H, DOM, MON, DOW = m.groups()

    # */N * * * *  → interval (분 단위)
    if M.startswith("*/") and H == "*" and DOM == "*" and MON == "*" and DOW == "*":
        try:
            n = int(M[2:])
            if n <= 0:
                raise ValueError
            return ParsedSchedule(kind="interval", interval_minutes=n)
        except ValueError:
            raise ScheduleParseError(f"cron */N 형식 오류: '{expr}'")

    # 나머지 패턴은 DOM과 MON이 *여야 함
    if DOM != "*" or MON != "*":
        raise ScheduleParseError(
            f"cron DOM/MON은 '*'만 지원: '{expr}' (단순화를 위한 제한)"
        )

    try:
        minute = int(M)
    except ValueError:
        raise ScheduleParseError(f"cron 분 필드 오류: '{expr}'")

    # M * * * *  → hourly
    if H == "*":
        return ParsedSchedule(kind="hourly", minute=minute)

    try:
        hour = int(H)
    except ValueError:
        raise ScheduleParseError(f"cron 시 필드 오류: '{expr}'")

    # M H * * *  → daily
    if DOW == "*":
        return ParsedSchedule(kind="daily", hour=hour, minute=minute)

    # DOW 파싱 (cron: 0=일 or 7=일, 1=월, ... 6=토)
    wd = _parse_cron_dow(DOW)
    return ParsedSchedule(kind="weekly", hour=hour, minute=minute, weekdays=wd)


def _parse_cron_dow(expr: str) -> list[int]:
    """cron DOW → Python weekday(0=월...6=일).

    cron: 0=일, 1=월, ..., 6=토, 7=일
    python: 0=월, 1=화, ..., 5=토, 6=일
    """
    def c2p(c: int) -> int:
        c = c % 7                # 7→0
        # cron: 0=Sun, 1=Mon, ..., 6=Sat
        # python: 0=Mon, 6=Sun
        return 6 if c == 0 else c - 1

    result: set[int] = set()
    for token in expr.split(","):
        token = token.strip()
        if "-" in token:
            a, b = token.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                raise ScheduleParseError(f"cron DOW 범위 오류: '{expr}'")
            for c in range(lo, hi + 1):
                result.add(c2p(c))
        else:
            try:
                result.add(c2p(int(token)))
            except ValueError:
                raise ScheduleParseError(f"cron DOW 값 오류: '{expr}'")
    return sorted(result)


# ──────────────────────────────────────────────────────────
# 베이스 등록기
# ──────────────────────────────────────────────────────────

JOB_PREFIX = "gdrive-sync-"


class BaseScheduler:
    """OS 독립 인터페이스."""

    def register(self, job: SchedulerJob, python_exe: str) -> str:
        raise NotImplementedError

    def unregister(self, name: str) -> bool:
        raise NotImplementedError

    def unregister_all(self) -> int:
        raise NotImplementedError

    def list_jobs(self) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def _command_line(python_exe: str, options: str) -> str:
        """`"<python>" -m gdrive_sync sync <options>` 형태 문자열 생성."""
        quoted_py = _quote(python_exe)
        opts = options.strip()
        return f'{quoted_py} -m gdrive_sync sync {opts}'.strip()


def _quote(s: str) -> str:
    """경로에 공백이 있으면 따옴표 처리."""
    if " " in s and not (s.startswith('"') and s.endswith('"')):
        return f'"{s}"'
    return s


# ──────────────────────────────────────────────────────────
# Windows: schtasks
# ──────────────────────────────────────────────────────────

class WindowsScheduler(BaseScheduler):

    def register(self, job: SchedulerJob, python_exe: str) -> str:
        schedule = parse_job(job)
        tn = JOB_PREFIX + job.name
        tr = self._command_line(python_exe, job.options)

        args = ["schtasks", "/create", "/tn", tn, "/tr", tr, "/f"]

        if schedule.kind == "daily":
            args += [
                "/sc", "DAILY",
                "/st", f"{schedule.hour:02d}:{schedule.minute:02d}",
            ]
        elif schedule.kind == "weekly":
            py_to_st = {0: "MON", 1: "TUE", 2: "WED", 3: "THU", 4: "FRI", 5: "SAT", 6: "SUN"}
            days = ",".join(py_to_st[d] for d in schedule.weekdays)
            args += [
                "/sc", "WEEKLY",
                "/d", days,
                "/st", f"{schedule.hour:02d}:{schedule.minute:02d}",
            ]
        elif schedule.kind == "hourly":
            # schtasks HOURLY는 시작 시각 기준으로 정각마다. minute은 시작 시각에 반영.
            # 임의의 "X시 M분에 시작해서 매 1시간" → /sc HOURLY /mo 1 /st HH:M
            # 시작 시각은 오늘의 다음 minute 기준
            from datetime import datetime, timedelta
            now = datetime.now()
            start_dt = now.replace(minute=schedule.minute, second=0, microsecond=0)
            if start_dt <= now:
                start_dt += timedelta(hours=1)
            args += [
                "/sc", "HOURLY",
                "/mo", "1",
                "/st", start_dt.strftime("%H:%M"),
            ]
        elif schedule.kind == "interval":
            args += [
                "/sc", "MINUTE",
                "/mo", str(schedule.interval_minutes),
            ]

        _run_and_raise(args)
        return tn

    def unregister(self, name: str) -> bool:
        tn = name if name.startswith(JOB_PREFIX) else JOB_PREFIX + name
        rc = subprocess.call(
            ["schtasks", "/delete", "/tn", tn, "/f"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return rc == 0

    def unregister_all(self) -> int:
        removed = 0
        for tn in self.list_jobs():
            if self.unregister(tn):
                removed += 1
        return removed

    def list_jobs(self) -> list[str]:
        try:
            out = subprocess.check_output(
                ["schtasks", "/query", "/fo", "CSV", "/nh"],
                stderr=subprocess.DEVNULL,
                text=True,
                errors="ignore",
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

        jobs = []
        for line in out.splitlines():
            # CSV: "TaskName","Next Run Time","Status",...
            parts = [p.strip('"') for p in line.split('","')]
            if not parts:
                continue
            name = parts[0].lstrip('"').strip()
            # schtasks는 \경로 포함 형태로 TaskName을 반환할 수 있음
            leaf = name.split("\\")[-1]
            if leaf.startswith(JOB_PREFIX):
                jobs.append(leaf)
        return sorted(set(jobs))


# ──────────────────────────────────────────────────────────
# macOS: launchd
# ──────────────────────────────────────────────────────────

class MacOSScheduler(BaseScheduler):

    @staticmethod
    def _label(name: str) -> str:
        return f"com.gdrive-sync.{name}"

    @staticmethod
    def _plist_path(name: str) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{MacOSScheduler._label(name)}.plist"

    def register(self, job: SchedulerJob, python_exe: str) -> str:
        schedule = parse_job(job)
        label = self._label(job.name)
        plist_path = self._plist_path(job.name)
        plist_path.parent.mkdir(parents=True, exist_ok=True)

        program_args = [python_exe, "-m", "gdrive_sync", "sync"]
        if job.options.strip():
            # options 문자열을 shell 스타일로 토큰화
            program_args += shlex.split(job.options)

        # 로그 경로 (재사용 쉬우라고 /tmp)
        log_dir = Path("/tmp")
        out_path = str(log_dir / f"{label}.out.log")
        err_path = str(log_dir / f"{label}.err.log")

        payload: dict = {
            "Label": label,
            "ProgramArguments": program_args,
            "StandardOutPath": out_path,
            "StandardErrorPath": err_path,
            "RunAtLoad": False,
        }

        # StartCalendarInterval 또는 StartInterval 설정
        if schedule.kind == "interval":
            payload["StartInterval"] = int(schedule.interval_minutes) * 60
        elif schedule.kind == "hourly":
            payload["StartCalendarInterval"] = {"Minute": schedule.minute}
        elif schedule.kind == "daily":
            payload["StartCalendarInterval"] = {
                "Hour": schedule.hour, "Minute": schedule.minute,
            }
        elif schedule.kind == "weekly":
            # launchd Weekday: 0=일, 1=월, ..., 6=토
            py_to_launchd = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
            payload["StartCalendarInterval"] = [
                {
                    "Weekday": py_to_launchd[d],
                    "Hour": schedule.hour,
                    "Minute": schedule.minute,
                }
                for d in schedule.weekdays
            ]

        # plist 저장
        with open(plist_path, "wb") as f:
            plistlib.dump(payload, f)
        try:
            plist_path.chmod(0o644)
        except OSError:
            pass

        # launchctl load (이미 로드돼 있으면 먼저 unload)
        subprocess.call(
            ["launchctl", "unload", str(plist_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _run_and_raise(["launchctl", "load", str(plist_path)])

        return label

    def unregister(self, name: str) -> bool:
        label = self._label(name.replace(JOB_PREFIX, "").replace("com.gdrive-sync.", ""))
        plist_path = self._plist_path(label.replace("com.gdrive-sync.", ""))
        ok = False
        if plist_path.exists():
            subprocess.call(
                ["launchctl", "unload", str(plist_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                plist_path.unlink()
                ok = True
            except OSError:
                pass
        return ok

    def unregister_all(self) -> int:
        removed = 0
        for label in self.list_jobs():
            short = label.replace("com.gdrive-sync.", "")
            if self.unregister(short):
                removed += 1
        return removed

    def list_jobs(self) -> list[str]:
        base = Path.home() / "Library" / "LaunchAgents"
        if not base.exists():
            return []
        return sorted(
            p.stem
            for p in base.glob("com.gdrive-sync.*.plist")
        )


# ──────────────────────────────────────────────────────────
# Linux: crontab
# ──────────────────────────────────────────────────────────

class LinuxScheduler(BaseScheduler):

    BEGIN_MARK = "# >>> gdrive-sync >>>"
    END_MARK = "# <<< gdrive-sync <<<"

    def register(self, job: SchedulerJob, python_exe: str) -> str:
        schedule = parse_job(job)
        cron_expr = self._to_cron(schedule)
        cmd = self._command_line(python_exe, job.options)

        entry = f"# job: {job.name}\n{cron_expr} {cmd}"

        current = self._read_crontab()
        new_lines, existing = self._extract_block(current)

        # 같은 이름 작업이 있으면 교체
        existing = [e for e in existing if f"# job: {job.name}" not in e]
        existing.append(entry)

        new_crontab = "\n".join(new_lines).rstrip()
        block = "\n".join([self.BEGIN_MARK] + existing + [self.END_MARK])
        if new_crontab:
            final = new_crontab + "\n\n" + block + "\n"
        else:
            final = block + "\n"
        self._write_crontab(final)
        return JOB_PREFIX + job.name

    def unregister(self, name: str) -> bool:
        short = name.replace(JOB_PREFIX, "")
        current = self._read_crontab()
        new_lines, existing = self._extract_block(current)

        before = len(existing)
        existing = [e for e in existing if f"# job: {short}" not in e]
        if len(existing) == before:
            return False

        new_crontab = "\n".join(new_lines).rstrip()
        if existing:
            block = "\n".join([self.BEGIN_MARK] + existing + [self.END_MARK])
            final = (new_crontab + "\n\n" + block + "\n") if new_crontab else (block + "\n")
        else:
            final = new_crontab + "\n" if new_crontab else ""
        self._write_crontab(final)
        return True

    def unregister_all(self) -> int:
        current = self._read_crontab()
        new_lines, existing = self._extract_block(current)
        removed = sum(1 for e in existing if e.startswith("# job:"))
        new_crontab = "\n".join(new_lines).rstrip()
        final = new_crontab + "\n" if new_crontab else ""
        self._write_crontab(final)
        return removed

    def list_jobs(self) -> list[str]:
        current = self._read_crontab()
        _, existing = self._extract_block(current)
        names = []
        for e in existing:
            # e는 "# job: name\n<cron line>" 형태의 멀티라인
            first_line = e.split("\n", 1)[0].strip()
            if first_line.startswith("# job:"):
                job_name = first_line.split("# job:", 1)[1].strip()
                names.append(JOB_PREFIX + job_name)
        return sorted(set(names))

    # ── helpers ──────────────────────────────

    @staticmethod
    def _to_cron(schedule: ParsedSchedule) -> str:
        if schedule.kind == "daily":
            return f"{schedule.minute} {schedule.hour} * * *"
        if schedule.kind == "weekly":
            # python 0(월) → cron 1, ..., python 6(일) → cron 0
            py_to_cron = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5, 5: 6, 6: 0}
            dow = ",".join(str(py_to_cron[d]) for d in sorted(schedule.weekdays))
            return f"{schedule.minute} {schedule.hour} * * {dow}"
        if schedule.kind == "hourly":
            return f"{schedule.minute} * * * *"
        if schedule.kind == "interval":
            return f"*/{schedule.interval_minutes} * * * *"
        raise ScheduleParseError(f"지원하지 않는 schedule kind: {schedule.kind}")

    def _read_crontab(self) -> list[str]:
        try:
            out = subprocess.check_output(
                ["crontab", "-l"],
                stderr=subprocess.DEVNULL, text=True, errors="ignore",
            )
            return out.splitlines()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return []

    def _write_crontab(self, content: str) -> None:
        if not content.strip():
            # 빈 crontab → crontab -r
            subprocess.call(
                ["crontab", "-r"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        p = subprocess.Popen(
            ["crontab", "-"],
            stdin=subprocess.PIPE, text=True,
        )
        p.communicate(content)
        if p.returncode != 0:
            raise RuntimeError("crontab 쓰기 실패")

    def _extract_block(self, lines: list[str]) -> tuple[list[str], list[str]]:
        """gdrive-sync 블록을 분리. (바깥 줄, 안쪽 항목들)을 반환."""
        outside: list[str] = []
        inside: list[str] = []
        in_block = False
        buffer_entry: list[str] = []
        for line in lines:
            if line.strip() == self.BEGIN_MARK:
                in_block = True
                continue
            if line.strip() == self.END_MARK:
                if buffer_entry:
                    inside.append("\n".join(buffer_entry))
                    buffer_entry = []
                in_block = False
                continue
            if in_block:
                if line.strip().startswith("# job:"):
                    if buffer_entry:
                        inside.append("\n".join(buffer_entry))
                    buffer_entry = [line]
                else:
                    buffer_entry.append(line)
            else:
                outside.append(line)
        if buffer_entry:
            inside.append("\n".join(buffer_entry))
        return outside, inside


# ──────────────────────────────────────────────────────────
# 팩토리
# ──────────────────────────────────────────────────────────

def get_scheduler() -> BaseScheduler:
    """현재 OS에 맞는 스케줄러 반환."""
    if sys.platform == "win32":
        return WindowsScheduler()
    if sys.platform == "darwin":
        return MacOSScheduler()
    return LinuxScheduler()


def default_python_executable() -> str:
    """현재 실행 중인 Python 절대 경로."""
    return str(Path(sys.executable).resolve())


# ──────────────────────────────────────────────────────────
# 내부
# ──────────────────────────────────────────────────────────

def _run_and_raise(args: list[str]) -> None:
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="ignore",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"명령 실패 ({proc.returncode}): {' '.join(args)}\n{proc.stderr.strip()}"
        )
