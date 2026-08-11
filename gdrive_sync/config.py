"""설정 파일(config.yaml) 파서.

- ~ (틸드) 홈 디렉토리 자동 해석으로 크로스플랫폼 호환 (Windows/macOS/Linux)
- hostname 기반 device_overrides 자동 적용
- v2 추가: performance / bandwidth / scheduler 섹션
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from gdrive_sync.utils import expand_path


DEFAULT_CONFIG_DIR = Path.home() / ".gdrive_sync"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"

# Google Drive Simple Upload 한도 (공식 5MB)
# 이 크기 미만 파일은 resumable 없이 한 번의 POST로 업로드 → 오버헤드 최소
SIMPLE_UPLOAD_MAX = 5 * 1024 * 1024   # 5 MB


@dataclass
class SyncPair:
    """동기화할 폴더 쌍 한 개."""
    local_path: Path        # 절대 경로 (expanduser 완료)
    remote_path: str        # 구글드라이브 상의 경로 ("업무/프로젝트" 형태)


@dataclass
class NetworkConfig:
    use_proxy: bool = False
    proxy_type: str = "http"        # http | socks4 | socks5
    proxy_host: str = ""
    proxy_port: int = 0
    proxy_username: str = ""
    proxy_password: str = ""
    use_system_proxy: bool = True
    timeout: int = 60
    max_retries: int = 3
    chunk_size: int = 8 * 1024 * 1024   # 기본 청크 크기 (8 MB)


@dataclass
class PerformanceConfig:
    """병렬 전송/청크 크기 자동 조절."""
    parallel_transfers: int = 5                   # 동시 전송 수 (1~10)
    chunk_size_auto: bool = True                  # 파일 크기별 자동 선택
    chunk_size_medium: int = 8 * 1024 * 1024      # 5MB~100MB: 8MB 청크
    chunk_size_large: int = 32 * 1024 * 1024      # 100MB 이상: 32MB 청크
    # 첫 동기화 / state 손실 시: 임계치(MB) 이상 파일은 MD5 대신
    # size + mtime(±2초) 매칭으로 SKIP 판정 — 디스크 전체 읽기 회피.
    # 0 이면 항상 MD5 (기존 동작).
    large_file_md5_skip_mb: int = 100


@dataclass
class BandwidthSchedule:
    """시간대별 대역폭 제한 규칙 하나."""
    name: str
    time_start: str        # "HH:MM"
    time_end: str          # "HH:MM" (자정 넘김 허용: start > end)
    upload_limit_mbps: float = 0       # MB/s, 0 = 무제한
    download_limit_mbps: float = 0
    weekdays: list[str] = field(default_factory=list)   # 빈 리스트 = 매일


@dataclass
class BandwidthConfig:
    enabled: bool = False
    upload_limit_mbps: float = 0      # 기본 제한 (스케줄 밖), 0 = 무제한
    download_limit_mbps: float = 0
    schedule: list[BandwidthSchedule] = field(default_factory=list)


@dataclass
class SchedulerJob:
    """OS 네이티브 스케줄러에 등록할 작업 하나.

    두 가지 형식 지원:
      A) 구조화:  type=daily/weekly/hourly + time/weekdays/minute
      B) cron:    cron="0 12 * * 1-5"  (기본 패턴만 지원)
    """
    name: str
    options: str = ""                             # 예: "--no-limit"
    cron: Optional[str] = None
    type: Optional[str] = None                    # daily | weekly | hourly | interval
    time: Optional[str] = None                    # "HH:MM" (daily/weekly)
    weekdays: list[str] = field(default_factory=list)   # weekly 전용
    minute: Optional[int] = None                  # hourly 전용
    interval_minutes: Optional[int] = None        # interval 전용


@dataclass
class SchedulerConfig:
    enabled: bool = False
    jobs: list[SchedulerJob] = field(default_factory=list)


@dataclass
class TrashConfig:
    """휴지통 설정.

    central=True (기본): ~/.gdrive_sync/trash/<폴더명>/<원래 상대경로>
        - 동기화 폴더(Downloads, Documents 등)를 오염시키지 않음
        - 사용자가 한 곳만 보면 됨
    central=False: 각 동기화 폴더의 .gdrive_sync_trash/ (구버전 동작)
    central_path: 빈 값이면 기본(~/.gdrive_sync/trash) 사용
    auto_cleanup_days: 0=비활성, N>0이면 동기화 후 N일 지난 휴지통 파일 자동 영구 삭제
    """
    central: bool = True
    central_path: str = ""
    auto_cleanup_days: int = 30


@dataclass
class Config:
    sync_pairs: list[SyncPair] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    conflict_policy: str = "newer_wins"   # newer_wins | local_wins | remote_wins | keep_both
    delete_policy: str = "trash"          # trash | permanent | skip
    network: NetworkConfig = field(default_factory=NetworkConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    bandwidth: BandwidthConfig = field(default_factory=BandwidthConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    trash: TrashConfig = field(default_factory=TrashConfig)
    log_level: str = "INFO"
    log_file: str = "gdrive_sync.log"
    # Drive가 '악성 의심'으로 분류해 다운로드를 차단한 파일도 본인 소유 시 강제 다운로드 허용
    # (False = 안전 기본값 / True = 사용자 책임 하 우회)
    acknowledge_abuse: bool = False
    # 동기화 시작 전 분절된 한글 파일명(NFD)을 자동으로 NFC로 정규화
    # macOS↔Windows 혼용 환경에서 파일명이 'ㅈㅜㅅㅣㅂㅈㅏ...'처럼 분절되는 문제 방지
    auto_normalize_filenames: bool = False

    # 메타 정보 (파일 경로 등)
    config_path: Path = field(default_factory=lambda: DEFAULT_CONFIG_PATH)


# ──────────────────────────────────────────────────────────
# 파일 I/O
# ──────────────────────────────────────────────────────────

def load_config(path: Path | None = None) -> Config:
    """config.yaml 로드 + device_overrides 적용."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"설정 파일을 찾을 수 없습니다: {path}\n"
            f"'gdrive-sync init' 명령으로 초기화하세요."
        )

    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    # device_overrides: hostname이 일치하면 병합
    hostname = socket.gethostname()
    overrides = (raw.get("device_overrides") or {}).get(hostname, {})
    if overrides:
        for k, v in overrides.items():
            raw[k] = v

    sync_pairs = [
        SyncPair(
            local_path=expand_path(p["local_path"]),
            remote_path=str(p["remote_path"]).strip("/"),
        )
        for p in (raw.get("sync_pairs") or [])
    ]

    net_raw = raw.get("network") or {}
    network = NetworkConfig(
        use_proxy=bool(net_raw.get("use_proxy", False)),
        proxy_type=str(net_raw.get("proxy_type", "http")),
        proxy_host=str(net_raw.get("proxy_host", "")),
        proxy_port=int(net_raw.get("proxy_port", 0)),
        proxy_username=str(net_raw.get("proxy_username", "")),
        proxy_password=str(net_raw.get("proxy_password", "")),
        use_system_proxy=bool(net_raw.get("use_system_proxy", True)),
        timeout=int(net_raw.get("timeout", 60)),
        max_retries=int(net_raw.get("max_retries", 3)),
        chunk_size=int(net_raw.get("chunk_size", 8 * 1024 * 1024)),
    )

    perf_raw = raw.get("performance") or {}
    performance = PerformanceConfig(
        parallel_transfers=_clamp_int(perf_raw.get("parallel_transfers", 5), 1, 10),
        chunk_size_auto=bool(perf_raw.get("chunk_size_auto", True)),
        chunk_size_medium=int(perf_raw.get("chunk_size_medium", 8 * 1024 * 1024)),
        chunk_size_large=int(perf_raw.get("chunk_size_large", 32 * 1024 * 1024)),
        large_file_md5_skip_mb=max(0, int(perf_raw.get("large_file_md5_skip_mb", 100))),
    )

    bw_raw = raw.get("bandwidth") or {}
    bw_schedule = [
        BandwidthSchedule(
            name=str(s.get("name", "")),
            time_start=str(s.get("time_start", "00:00")),
            time_end=str(s.get("time_end", "23:59")),
            upload_limit_mbps=float(s.get("upload_limit_mbps", 0) or 0),
            download_limit_mbps=float(s.get("download_limit_mbps", 0) or 0),
            weekdays=list(s.get("weekdays") or []),
        )
        for s in (bw_raw.get("schedule") or [])
    ]
    bandwidth = BandwidthConfig(
        enabled=bool(bw_raw.get("enabled", False)),
        upload_limit_mbps=float(bw_raw.get("upload_limit_mbps", 0) or 0),
        download_limit_mbps=float(bw_raw.get("download_limit_mbps", 0) or 0),
        schedule=bw_schedule,
    )

    sch_raw = raw.get("scheduler") or {}
    sch_jobs = [
        SchedulerJob(
            name=str(j.get("name", "")),
            options=str(j.get("options", "") or ""),
            cron=j.get("cron"),
            type=j.get("type"),
            time=j.get("time"),
            weekdays=list(j.get("weekdays") or []),
            minute=j.get("minute"),
            interval_minutes=j.get("interval_minutes"),
        )
        for j in (sch_raw.get("jobs") or [])
    ]
    scheduler_cfg = SchedulerConfig(
        enabled=bool(sch_raw.get("enabled", False)),
        jobs=sch_jobs,
    )

    trash_raw = raw.get("trash") or {}
    trash_cfg = TrashConfig(
        central=bool(trash_raw.get("central", True)),
        central_path=str(trash_raw.get("central_path", "") or ""),
        auto_cleanup_days=_clamp_int(trash_raw.get("auto_cleanup_days", 30), 0, 365),
    )

    return Config(
        sync_pairs=sync_pairs,
        exclude_patterns=list(raw.get("exclude_patterns") or []),
        conflict_policy=str(raw.get("conflict_policy", "newer_wins")),
        delete_policy=str(raw.get("delete_policy", "trash")),
        network=network,
        performance=performance,
        bandwidth=bandwidth,
        scheduler=scheduler_cfg,
        trash=trash_cfg,
        log_level=str(raw.get("log_level", "INFO")),
        log_file=str(raw.get("log_file", "gdrive_sync.log")),
        acknowledge_abuse=bool(raw.get("acknowledge_abuse", False)),
        auto_normalize_filenames=bool(raw.get("auto_normalize_filenames", False)),
        config_path=path,
    )


def _clamp_int(v: Any, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = lo
    return max(lo, min(hi, n))


def save_config(cfg_dict: dict[str, Any], path: Path | None = None) -> Path:
    """Python dict를 yaml로 저장. init 명령에서 사용."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            cfg_dict,
            f,
            allow_unicode=True,
            sort_keys=False,
            indent=2,
        )
    return path


def default_config_template() -> dict[str, Any]:
    """init 시 사용할 기본 설정 템플릿."""
    return {
        "sync_pairs": [
            {"local_path": "~/GDriveSync", "remote_path": "동기화테스트"},
        ],
        "exclude_patterns": [
            "*.tmp", "*.log", "~$*", ".DS_Store", "Thumbs.db",
            "desktop.ini", "__pycache__/", ".git/", ".gdrive_sync_*", "*.lnk",
        ],
        "conflict_policy": "newer_wins",
        "delete_policy": "trash",
        "network": {
            "use_proxy": False,
            "proxy_type": "http",
            "proxy_host": "",
            "proxy_port": 0,
            "proxy_username": "",
            "proxy_password": "",
            "use_system_proxy": True,
            "timeout": 60,
            "max_retries": 3,
            "chunk_size": 8 * 1024 * 1024,
        },
        "performance": {
            "parallel_transfers": 5,
            "chunk_size_auto": True,
            "chunk_size_medium": 8 * 1024 * 1024,
            "chunk_size_large": 32 * 1024 * 1024,
            "large_file_md5_skip_mb": 100,
        },
        "bandwidth": {
            "enabled": False,
            "upload_limit_mbps": 0,
            "download_limit_mbps": 0,
            "schedule": [],
        },
        "scheduler": {
            "enabled": False,
            "jobs": [],
        },
        "trash": {
            "central": True,                 # True: ~/.gdrive_sync/trash 한 곳에 모음
            "central_path": "",              # 빈 값 = 기본 위치
            "auto_cleanup_days": 30,         # N일 지난 파일 자동 영구 삭제 (0=비활성)
        },
        "log_level": "INFO",
        "log_file": "gdrive_sync.log",
        "auto_normalize_filenames": False,  # 동기화 전 NFD→NFC 자동 정규화
    }
