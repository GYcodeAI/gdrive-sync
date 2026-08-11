"""충돌 해결 정책.

양쪽이 모두 변경된 경우 어느 쪽을 우선할지 결정.
- newer_wins  : 최신 수정시간 우선
- local_wins  : 항상 로컬
- remote_wins : 항상 리모트
- keep_both   : 양쪽 모두 보존 (충돌 파일명 생성)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Optional

from gdrive_sync.utils import parse_rfc3339, utcnow_iso


class ConflictResolution(str, Enum):
    UPLOAD = "upload"           # 로컬 → 리모트로 덮어씀
    DOWNLOAD = "download"       # 리모트 → 로컬로 덮어씀
    KEEP_BOTH = "keep_both"     # 둘 다 보존


@dataclass
class ConflictDecision:
    action: ConflictResolution
    rename_to: Optional[str] = None     # keep_both일 때 한 쪽을 이 이름으로 rename


def resolve(
    policy: str,
    rel_path: str,
    local_mtime_iso: str,
    remote_mtime_iso: str,
) -> ConflictDecision:
    """충돌 정책에 따른 결정 반환."""
    policy = (policy or "newer_wins").lower()

    if policy == "local_wins":
        return ConflictDecision(ConflictResolution.UPLOAD)
    if policy == "remote_wins":
        return ConflictDecision(ConflictResolution.DOWNLOAD)
    if policy == "keep_both":
        return ConflictDecision(
            ConflictResolution.KEEP_BOTH,
            rename_to=_conflict_name(rel_path),
        )

    # newer_wins (기본)
    l = parse_rfc3339(local_mtime_iso) if local_mtime_iso else datetime.fromtimestamp(0, tz=timezone.utc)
    r = parse_rfc3339(remote_mtime_iso) if remote_mtime_iso else datetime.fromtimestamp(0, tz=timezone.utc)
    if l >= r:
        return ConflictDecision(ConflictResolution.UPLOAD)
    return ConflictDecision(ConflictResolution.DOWNLOAD)


def _conflict_name(rel_path: str) -> str:
    """파일명_conflict_YYYYMMDD_HHMMSS.확장자"""
    p = PurePosixPath(rel_path)
    stem = p.stem
    suffix = p.suffix
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    new_name = f"{stem}_conflict_{ts}{suffix}"
    parent = str(p.parent)
    if parent in (".", ""):
        return new_name
    return f"{parent}/{new_name}"
