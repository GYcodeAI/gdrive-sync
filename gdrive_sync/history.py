"""동기화 히스토리 기록.

매 동기화 완료 시 결과를 ~/.gdrive_sync/history.json에 누적 저장.
GUI의 통계 패널과 CLI status 명령에서 활용.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


HISTORY_PATH = Path.home() / ".gdrive_sync" / "history.json"
MAX_RECORDS = 500  # 최대 보관 건수 (오래된 것부터 삭제)


@dataclass
class SyncRecord:
    """동기화 1회 기록."""
    timestamp: str                  # ISO 8601 Z
    uploaded: int = 0
    uploaded_bytes: int = 0
    downloaded: int = 0
    downloaded_bytes: int = 0
    deleted: int = 0
    # 양쪽 모두 사라져 state 만 정리한 항목 수 (REMOVE_STATE).
    # 대량 발생은 Drive list_tree 일시 누락 같은 이상 신호의 후행 지표.
    removed_state: int = 0
    conflicts: int = 0
    errors: int = 0
    skipped: int = 0
    elapsed_sec: float = 0.0
    pairs_count: int = 0            # 동기화 쌍 수
    dry_run: bool = False
    total_files_tracked: int = 0    # 동기화 후 총 추적 파일 수
    pair_paths: list[str] = field(default_factory=list)  # 이번에 동기화한 로컬 폴더 경로들
    action_summary: dict[str, int] = field(default_factory=dict)  # ActionType별 건수


def load_history(path: Optional[Path] = None) -> list[SyncRecord]:
    """히스토리 로드. 없거나 손상 시 빈 리스트."""
    p = path or HISTORY_PATH
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return [
            SyncRecord(**{k: r.get(k, d) for k, d in _DEFAULTS.items()})
            for r in raw
        ]
    except (json.JSONDecodeError, TypeError, OSError):
        return []


def append_record(record: SyncRecord, path: Optional[Path] = None) -> None:
    """새 기록 추가. MAX_RECORDS 초과 시 오래된 것 삭제. 원자적 쓰기."""
    p = path or HISTORY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)

    history = load_history(p)
    history.append(record)

    # 오래된 것 삭제 (앞에서부터)
    if len(history) > MAX_RECORDS:
        history = history[-MAX_RECORDS:]

    # 원자적 쓰기
    fd, tmp = tempfile.mkstemp(suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([asdict(r) for r in history], f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def format_timestamp_local(ts: str) -> str:
    """UTC ISO 타임스탬프를 로컬 시각 문자열로 변환.

    저장값은 항상 UTC(Z 접미사)이므로, 표시 시 로컬 시각으로 변환해야 한다.
    파싱 실패 시 원본 문자열을 그대로 반환(이전 데이터 호환).
    """
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, AttributeError):
        return ts[:19].replace("T", " ")


def get_stats(history: list[SyncRecord]) -> dict:
    """히스토리에서 통계 요약 생성."""
    if not history:
        return {
            "total_syncs": 0,
            "total_uploaded": 0,
            "total_downloaded": 0,
            "total_uploaded_bytes": 0,
            "total_downloaded_bytes": 0,
            "total_errors": 0,
            "total_elapsed": 0.0,
            "last_sync": None,
            "avg_elapsed": 0.0,
            "success_rate": 0.0,
        }

    # dry-run 제외
    real = [r for r in history if not r.dry_run]
    if not real:
        real = history  # dry-run만 있으면 그거라도

    total_up = sum(r.uploaded for r in real)
    total_dn = sum(r.downloaded for r in real)
    total_up_bytes = sum(r.uploaded_bytes for r in real)
    total_dn_bytes = sum(r.downloaded_bytes for r in real)
    total_err = sum(r.errors for r in real)
    total_elapsed = sum(r.elapsed_sec for r in real)
    total_syncs = len(real)
    error_free = sum(1 for r in real if r.errors == 0)

    return {
        "total_syncs": total_syncs,
        "total_uploaded": total_up,
        "total_downloaded": total_dn,
        "total_uploaded_bytes": total_up_bytes,
        "total_downloaded_bytes": total_dn_bytes,
        "total_errors": total_err,
        "total_elapsed": total_elapsed,
        "last_sync": real[-1].timestamp if real else None,
        "avg_elapsed": total_elapsed / total_syncs if total_syncs else 0,
        "success_rate": (error_free / total_syncs * 100) if total_syncs else 0,
    }


# 기본값 (이전 버전 호환)
_DEFAULTS = {
    "timestamp": "",
    "uploaded": 0,
    "uploaded_bytes": 0,
    "downloaded": 0,
    "downloaded_bytes": 0,
    "deleted": 0,
    "removed_state": 0,
    "conflicts": 0,
    "errors": 0,
    "skipped": 0,
    "elapsed_sec": 0.0,
    "pairs_count": 0,
    "dry_run": False,
    "total_files_tracked": 0,
    "pair_paths": [],
    "action_summary": {},
}
