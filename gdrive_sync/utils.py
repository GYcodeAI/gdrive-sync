r"""공용 유틸리티: 경로/해시/시간 처리.

- 크로스플랫폼 경로 정규화 (~, \\?\ 접두사 등)
- MD5 해시 (청크 단위로 대용량 지원)
- UTC 시간 파싱/포맷
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


# ──────────────────────────────────────────────────────────
# 경로 처리
# ──────────────────────────────────────────────────────────

def expand_path(p: str | os.PathLike) -> Path:
    """~, 환경변수, 상대경로를 해석한 절대 Path 반환."""
    return Path(os.path.expandvars(os.fspath(p))).expanduser().resolve()


def to_long_path(p: Path) -> str:
    r"""Windows 260자 제한 우회용 \\?\ 접두사 적용.

    Windows가 아니면 그대로 str 반환.
    """
    s = str(p)
    if sys.platform == "win32" and len(s) > 240 and not s.startswith("\\\\?\\"):
        # UNC는 \\?\UNC\server\share 형태
        if s.startswith("\\\\"):
            return "\\\\?\\UNC\\" + s.lstrip("\\")
        return "\\\\?\\" + s
    return s


def to_posix(p: str | Path) -> str:
    """OS 경로를 구글드라이브용 POSIX 경로(슬래시)로 변환."""
    return str(PurePosixPath(*Path(p).parts)).replace("\\", "/")


def split_remote_path(remote_path: str) -> list[str]:
    """'업무/프로젝트/2026' → ['업무','프로젝트','2026']."""
    return [p for p in remote_path.replace("\\", "/").split("/") if p]


# ──────────────────────────────────────────────────────────
# 제외 패턴 매칭
# ──────────────────────────────────────────────────────────

_BRACKET_RE = re.compile(r'\[(!?)([^\]]*)\]')


def _escape_literal_brackets(pat: str) -> str:
    """fnmatch 패턴에서 리터럴 브라켓을 이스케이프.

    [THUMBNAIL], [Conflict Copy] 처럼 단어/구문이 들어간 브라켓은
    fnmatch가 문자 클래스로 오해석하므로 [[] ... []] 형태로 변환한다.

    진짜 문자 클래스는 그대로 둔다:
      - [!abc]  부정 셋
      - [a-z]   범위
      - [ab]    2자 이하 명시 셋
    """
    # \[ 형식 이스케이프도 처리 (config에서 \[THUMBNAIL\]* 로 쓴 경우)
    pat = pat.replace("\\[", "[").replace("\\]", "]")

    def replace(m: re.Match) -> str:
        neg, inner = m.group(1), m.group(2)
        if neg or "-" in inner or len(inner) <= 2:
            return m.group(0)
        return "[[]" + inner + "[]]"
    return _BRACKET_RE.sub(replace, pat)


def matches_any(rel_path: str, patterns: Iterable[str]) -> bool:
    """상대 경로가 제외 패턴 중 하나라도 매칭되면 True.

    - glob 문법 (fnmatch)
    - 끝에 /가 붙은 패턴은 디렉토리로 취급 (경로 내 어느 부분이라도 매치)
    - [THUMBNAIL]* 같은 리터럴 브라켓 패턴을 올바르게 처리 (`\\[` 이스케이프도 지원)
    - 대소문자 구분(fnmatchcase): `fnmatch.fnmatch`는 OS의 normcase를 거쳐
      Windows에서만 대소문자를 무시 → 플랫폼마다 제외 결과가 달라짐.
      또한 `[a-z]` 문자 클래스가 Windows에서 'A'에도 매치되는 부작용 발생.
      크로스플랫폼 일관성을 위해 항상 대소문자 구분 매칭을 사용.
    """
    parts = rel_path.replace("\\", "/").split("/")
    name = parts[-1]
    for pat in patterns:
        pat_norm = _escape_literal_brackets(pat.strip())
        if not pat_norm:
            continue
        if pat_norm.endswith("/"):
            # 디렉토리 패턴: 경로 상 어디든 매치하는지 확인
            dir_pat = pat_norm.rstrip("/")
            if any(fnmatch.fnmatchcase(seg, dir_pat) for seg in parts[:-1]):
                return True
            continue
        # 파일 패턴: 파일명 혹은 전체 경로
        if fnmatch.fnmatchcase(name, pat_norm):
            return True
        if fnmatch.fnmatchcase(rel_path, pat_norm):
            return True
    return False


# ──────────────────────────────────────────────────────────
# 해시
# ──────────────────────────────────────────────────────────

def md5_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """대용량 파일 MD5 (16진 hex)."""
    h = hashlib.md5()
    with open(to_long_path(path), "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ──────────────────────────────────────────────────────────
# 시간 처리 (모두 UTC)
# ──────────────────────────────────────────────────────────

def utcnow_iso() -> str:
    """현재 UTC 시간을 ISO 8601 Z 포맷으로 반환."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mtime_to_iso(mtime: float) -> str:
    """os.path.getmtime()의 float → ISO 8601 Z."""
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_rfc3339(s: str) -> datetime:
    """Drive API의 modifiedTime (RFC3339) → aware datetime (UTC)."""
    if not s:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # 밀리초가 있는 경우 등 fallback
        dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def dt_to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mtime_close(iso_a: str, iso_b: str, tol_sec: float = 2.0) -> bool:
    """두 ISO/RFC3339 시각이 허용 오차 내에 있는지.

    파일시스템 mtime 정밀도(FAT: 2초, NTFS: 100ns)와 Drive modifiedTime(밀리초)
    의 표현 차이를 흡수. C-2(큰 파일 size+mtime 매칭)에서 사용.
    빈 문자열 한쪽이라도 있으면 False.
    """
    if not iso_a or not iso_b:
        return False
    try:
        a = parse_rfc3339(iso_a)
        b = parse_rfc3339(iso_b)
    except Exception:
        return False
    return abs((a - b).total_seconds()) <= tol_sec


# ──────────────────────────────────────────────────────────
# 사람이 읽기 좋은 크기
# ──────────────────────────────────────────────────────────

def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def format_duration(seconds: float) -> str:
    """초 단위를 한국어로 '1시간 30분' 형식으로 포맷."""
    if seconds < 1:
        return "1초 미만"
    if seconds < 60:
        return f"{int(seconds)}초"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}분 {s}초" if s else f"{m}분"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}시간 {m}분" if m else f"{h}시간"
