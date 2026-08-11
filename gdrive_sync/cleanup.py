"""gdrive-sync 설치 진단(doctor) 및 완전 제거(uninstall).

프로그램이 생성하는 모든 파일/설정을 추적하고 정리.
사용자 데이터(동기화된 실제 파일)는 절대 건드리지 않음.

관리 대상:
- ~/.gdrive_sync/ (config, token, log, history, gui_state)
- 각 동기화 폴더/.gdrive_sync_state.json (숨김 상태 파일)
- 각 동기화 폴더/.gdrive_sync_trash/ (로컬 휴지통)
- OS 스케줄러 (Windows schtasks / macOS launchd / Linux crontab)
- Python 패키지 (pip uninstall)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gdrive_sync.config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH, load_config
from gdrive_sync.state import state_path_for
from gdrive_sync.utils import human_size


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 진단 결과 구조
# ──────────────────────────────────────────────────────────

@dataclass
class DiagItem:
    """진단 항목 하나."""
    category: str           # "config" | "state" | "trash" | "scheduler" | "package" | "log"
    path_or_name: str       # 파일 경로 또는 이름
    exists: bool            # 존재 여부
    size_bytes: int = 0     # 파일/폴더 크기
    detail: str = ""        # 추가 정보
    cleanable: bool = False # doctor에서 정리 가능 여부


@dataclass
class DiagReport:
    """전체 진단 결과."""
    items: list[DiagItem] = field(default_factory=list)

    @property
    def total_cleanable_bytes(self) -> int:
        return sum(i.size_bytes for i in self.items if i.cleanable and i.exists)

    @property
    def cleanable_items(self) -> list[DiagItem]:
        return [i for i in self.items if i.cleanable and i.exists]


# ──────────────────────────────────────────────────────────
# 진단 (doctor)
# ──────────────────────────────────────────────────────────

def diagnose() -> DiagReport:
    """현재 설치 상태를 진단해 보고서 생성."""
    report = DiagReport()

    # 1) 설정 디렉토리
    _check_config_dir(report)

    # 2) 동기화 폴더별 state/trash
    _check_sync_folders(report)

    # 3) OS 스케줄러
    _check_scheduler(report)

    # 4) Python 패키지
    _check_package(report)

    return report


def _check_config_dir(report: DiagReport) -> None:
    """~/.gdrive_sync/ 내부 파일 점검."""
    config_dir = DEFAULT_CONFIG_DIR

    files = {
        "config.yaml": ("config", False),        # 설정 — doctor에서 안 지움
        "token.json": ("config", False),          # 토큰 — doctor에서 안 지움
        "gui_state.json": ("config", False),      # GUI 상태 — 작음
        # history.json은 사용자 통계 기록 — `--clean`으로는 안 지우고
        # 명시적 `--clean-history`만 삭제. 일반 정리에 휘말리지 않도록 cleanable=False.
        "history.json": ("log", False),
        "gdrive_sync.log": ("log", True),         # 로그 — 정리 가능
    }

    for name, (category, cleanable) in files.items():
        p = config_dir / name
        size = _safe_size(p)
        detail = human_size(size) if size else ""
        # token.json은 발급(혹은 갱신) 후 경과일을 함께 표시 — 만료 임박 사전 인지용
        if name == "token.json" and p.exists():
            age_str = _format_token_age(p)
            if age_str:
                detail = f"{detail}  {age_str}" if detail else age_str
        report.items.append(DiagItem(
            category=category,
            path_or_name=str(p),
            exists=p.exists(),
            size_bytes=size,
            detail=detail,
            cleanable=cleanable,
        ))


def _format_token_age(token_path: Path) -> str:
    """token.json mtime 기반 발급/갱신 후 경과 표시.

    OAuth 앱이 프로덕션 게시 상태면 자동 만료 없으나, 만일을 대비해
    오래된 토큰일수록 사용자에게 시각적 경고를 한 줄로 제공.
    """
    import time as _time
    try:
        mt = token_path.stat().st_mtime
    except OSError:
        return ""
    elapsed_days = (_time.time() - mt) / 86400
    days = int(elapsed_days)
    if elapsed_days < 1:
        # 24시간 이내
        hours = int(elapsed_days * 24)
        return f"발급 후 {hours}시간 (정상)"
    if days <= 5:
        return f"발급 후 {days}일 (정상)"
    if days <= 6:
        # 테스트 모드 OAuth 앱은 7일 만료. 임박.
        return f"⚠ 발급 후 {days}일 (만료 임박 가능 — 테스트 모드 앱이면 7일 후 만료)"
    if days < 30:
        return f"발급 후 {days}일 (프로덕션 앱이면 정상, 테스트 모드면 이미 만료)"
    return f"발급 후 {days}일 (오래됨 — 6개월 미사용 시 Google이 무효화)"


def _check_sync_folders(report: DiagReport) -> None:
    """각 동기화 폴더의 state 파일과 trash 폴더 점검."""
    try:
        cfg = load_config()
    except FileNotFoundError:
        return

    for pair in cfg.sync_pairs:
        # state 파일
        sp = state_path_for(pair.local_path)
        size = _safe_size(sp)
        report.items.append(DiagItem(
            category="state",
            path_or_name=str(sp),
            exists=sp.exists(),
            size_bytes=size,
            detail=human_size(size) if size else "",
            cleanable=False,   # state는 doctor에서 안 지움 (reset-state 사용)
        ))

        # 구버전 폴더별 trash
        trash = pair.local_path / ".gdrive_sync_trash"
        if trash.exists():
            size = _dir_size(trash)
            count = sum(1 for _ in trash.rglob("*") if _.is_file())
            report.items.append(DiagItem(
                category="trash",
                path_or_name=str(trash),
                exists=True,
                size_bytes=size,
                detail=f"{count}개 파일, {human_size(size)}  [구버전 위치]",
                cleanable=True,
            ))

    # 신버전 중앙 trash (한 번만 보고)
    central = _resolve_central_trash_dir(cfg.trash)
    if central.exists():
        size = _dir_size(central)
        count = sum(1 for _ in central.rglob("*") if _.is_file())
        report.items.append(DiagItem(
            category="trash",
            path_or_name=str(central),
            exists=True,
            size_bytes=size,
            detail=f"{count}개 파일, {human_size(size)}  [중앙]",
            cleanable=True,
        ))


def _check_scheduler(report: DiagReport) -> None:
    """OS 스케줄러 등록 상태 점검."""
    try:
        from gdrive_sync.scheduler import get_scheduler
        s = get_scheduler()
        jobs = s.list_jobs()
        for job_name in jobs:
            report.items.append(DiagItem(
                category="scheduler",
                path_or_name=job_name,
                exists=True,
                detail=f"OS 예약 작업 ({s.__class__.__name__})",
                cleanable=False,  # doctor에서는 안 지움. uninstall에서 지움.
            ))
        if not jobs:
            report.items.append(DiagItem(
                category="scheduler",
                path_or_name="(없음)",
                exists=False,
                detail="등록된 예약 작업 없음",
            ))
    except Exception as e:
        report.items.append(DiagItem(
            category="scheduler",
            path_or_name="(확인 실패)",
            exists=False,
            detail=str(e),
        ))


def _check_package(report: DiagReport) -> None:
    """pip 패키지 설치 상태 점검."""
    try:
        import gdrive_sync
        ver = gdrive_sync.__version__
        report.items.append(DiagItem(
            category="package",
            path_or_name=f"gdrive-sync {ver}",
            exists=True,
            detail=f"Python {sys.version.split()[0]}, {sys.executable}",
        ))
    except ImportError:
        report.items.append(DiagItem(
            category="package",
            path_or_name="gdrive-sync (미설치)",
            exists=False,
        ))


# ──────────────────────────────────────────────────────────
# 정리 (doctor clean)
# ──────────────────────────────────────────────────────────

def clean_logs() -> tuple[int, int]:
    """로그 파일만 삭제. (삭제 건수, 바이트) 반환.

    NOTE: history.json은 사용자 통계 기록이라 삭제 대상에서 제외됨.
    히스토리를 지우려면 clean_history() 또는 `doctor --clean-history` 사용.
    """
    count = 0
    total_bytes = 0
    for name in ("gdrive_sync.log",):
        p = DEFAULT_CONFIG_DIR / name
        if p.exists():
            total_bytes += _safe_size(p)
            p.unlink()
            count += 1
    return count, total_bytes


def clean_history() -> tuple[int, int]:
    """history.json 삭제. (삭제 건수, 바이트) 반환.

    히스토리는 동기화 동작에 영향 없는 통계 기록일 뿐이므로 안전하게 지울 수 있다.
    다만 GUI '동기화 히스토리' 다이얼로그의 과거 기록이 모두 사라진다.
    """
    p = DEFAULT_CONFIG_DIR / "history.json"
    if p.exists():
        size = _safe_size(p)
        p.unlink()
        return 1, size
    return 0, 0


def _resolve_central_trash_dir(trash_cfg) -> Path:
    """TrashConfig에서 중앙 trash 디렉터리 경로 도출."""
    if trash_cfg.central_path:
        return Path(trash_cfg.central_path).expanduser()
    return Path.home() / ".gdrive_sync" / "trash"


def clean_trash_all() -> tuple[int, int]:
    """구버전 폴더별 .gdrive_sync_trash + 신버전 중앙 trash 모두 삭제.

    Returns: (삭제된 폴더 수, 절약된 바이트)
    """
    count = 0
    total_bytes = 0
    try:
        cfg = load_config()
    except FileNotFoundError:
        return 0, 0

    # 구버전: 폴더별 trash
    for pair in cfg.sync_pairs:
        trash = pair.local_path / ".gdrive_sync_trash"
        if trash.exists():
            total_bytes += _dir_size(trash)
            shutil.rmtree(str(trash), ignore_errors=True)
            count += 1

    # 신버전: 중앙 trash
    central = _resolve_central_trash_dir(cfg.trash)
    if central.exists():
        total_bytes += _dir_size(central)
        shutil.rmtree(str(central), ignore_errors=True)
        count += 1

    return count, total_bytes


def cleanup_old_trash(days: int = 0) -> tuple[int, int]:
    """N일 지난 휴지통 파일 자동 영구 삭제.

    days <= 0이면 아무것도 안 함.
    구버전 폴더별 trash + 신버전 중앙 trash 모두 대상.
    Returns: (삭제된 파일 수, 절약된 바이트)
    """
    if days <= 0:
        return 0, 0

    try:
        cfg = load_config()
    except FileNotFoundError:
        return 0, 0

    cutoff = time.time() - (days * 86400)
    deleted_count = 0
    deleted_bytes = 0

    trash_dirs: list[Path] = []
    for pair in cfg.sync_pairs:
        t = pair.local_path / ".gdrive_sync_trash"
        if t.exists():
            trash_dirs.append(t)
    central = _resolve_central_trash_dir(cfg.trash)
    if central.exists():
        trash_dirs.append(central)

    for trash_dir in trash_dirs:
        # 1) 오래된 파일 삭제
        for f in list(trash_dir.rglob("*")):
            if f.is_file():
                try:
                    if f.stat().st_mtime < cutoff:
                        sz = f.stat().st_size
                        f.unlink()
                        deleted_count += 1
                        deleted_bytes += sz
                except OSError:
                    pass
        # 2) 빈 폴더 청소 (안쪽부터)
        for d in sorted(trash_dir.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass

    return deleted_count, deleted_bytes


# ──────────────────────────────────────────────────────────
# 완전 제거 (uninstall)
# ──────────────────────────────────────────────────────────

@dataclass
class UninstallStep:
    name: str
    detail: str
    success: bool = False
    error: Optional[str] = None


def uninstall_all(
    remove_config: bool = True,
    remove_states: bool = True,
    remove_trash: bool = True,
    remove_scheduler: bool = True,
    remove_package: bool = True,
    progress_cb=None,
) -> list[UninstallStep]:
    """프로그램의 모든 흔적 제거.

    사용자 데이터(동기화된 파일)는 절대 건드리지 않음.
    credentials.json은 보안상 자동 삭제하지 않음 (안내만).

    progress_cb: (step_index, total_steps, step_name) → None
    """
    steps: list[UninstallStep] = []
    total = sum([remove_states, remove_trash, remove_scheduler, remove_config, remove_package])
    step_idx = 0

    def _report(name, detail):
        nonlocal step_idx
        step_idx += 1
        if progress_cb:
            progress_cb(step_idx, total, name)
        return UninstallStep(name=name, detail=detail)

    # 1) state 파일 삭제
    if remove_states:
        step = _report("동기화 상태 파일", ".gdrive_sync_state.json")
        try:
            removed = _remove_all_state_files()
            step.detail = f"{removed}개 삭제"
            step.success = True
        except Exception as e:
            step.error = str(e)
        steps.append(step)

    # 2) 휴지통 삭제
    if remove_trash:
        step = _report("로컬 휴지통", ".gdrive_sync_trash/")
        try:
            count, size = clean_trash_all()
            step.detail = f"{count}개 폴더 삭제 ({human_size(size)})"
            step.success = True
        except Exception as e:
            step.error = str(e)
        steps.append(step)

    # 3) OS 스케줄러 해제
    if remove_scheduler:
        step = _report("OS 예약 작업", "schtasks / launchd / cron")
        try:
            removed = _remove_all_scheduler_jobs()
            step.detail = f"{removed}개 작업 해제"
            step.success = True
        except Exception as e:
            step.error = str(e)
        steps.append(step)

    # 4) 설정 디렉토리 삭제
    if remove_config:
        step = _report("설정 디렉토리", str(DEFAULT_CONFIG_DIR))
        try:
            if DEFAULT_CONFIG_DIR.exists():
                shutil.rmtree(str(DEFAULT_CONFIG_DIR), ignore_errors=True)
            step.detail = f"{DEFAULT_CONFIG_DIR} 삭제 완료"
            step.success = True
        except Exception as e:
            step.error = str(e)
        steps.append(step)

    # 5) pip uninstall
    if remove_package:
        step = _report("Python 패키지", "pip uninstall gdrive-sync")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", "gdrive-sync"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                step.detail = "pip uninstall 완료"
                step.success = True
            else:
                step.error = result.stderr.strip() or "pip uninstall 실패"
        except Exception as e:
            step.error = str(e)
        steps.append(step)

    return steps


def _remove_all_state_files() -> int:
    """모든 동기화 폴더의 state 파일 삭제."""
    count = 0
    try:
        cfg = load_config()
    except FileNotFoundError:
        return 0
    for pair in cfg.sync_pairs:
        sp = state_path_for(pair.local_path)
        if sp.exists():
            sp.unlink()
            count += 1
    return count


def _remove_all_scheduler_jobs() -> int:
    """OS에 등록된 gdrive-sync 예약 작업 모두 해제."""
    try:
        from gdrive_sync.scheduler import get_scheduler
        s = get_scheduler()
        return s.unregister_all()
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────
# 유틸
# ──────────────────────────────────────────────────────────

def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else 0
    except OSError:
        return 0


def _dir_size(d: Path) -> int:
    total = 0
    try:
        for f in d.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    except OSError:
        pass
    return total
