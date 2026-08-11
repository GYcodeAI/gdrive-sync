"""로컬 파일 시스템 스캐너.

- 루트 디렉토리 하위의 모든 파일을 재귀 탐색
- 제외 패턴 적용
- size/mtime/md5 수집 (md5는 lazy 계산)
- 심볼릭 링크 기본 무시
"""

from __future__ import annotations

import logging
import os
import re
import stat as _stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# MS Word/한글/백신·DLP 프로그램이 문서 스캔 중 만드는 5~8자 대문자(+숫자) 임시파일
# (예: LHZ5DWIU.DOCX, JRZ5L.DOCX, ENNEO.DOCX)
# - 이 프로젝트 사용자는 평소 영문 파일명을 쓰지 않으므로, 숫자 유무와 무관하게
#   짧은 대문자 영단어 조합은 임시파일로 간주해 제외한다 (사용자 결정, 2026-08-06).
#   실제로 이런 이름의 파일이 필요하면 파일명에 한글/숫자/공백을 섞거나
#   '개별 동기화'로 수동 처리하면 된다.
# - _TEMP_NAME_ALLOWLIST: README 등 관례적으로 흔히 쓰이는 실제 파일명은 예외로 보존.
# - 확장자: 대소문자 무시
_RE_OFFICE_TEMP = re.compile(
    r"^[A-Z0-9]{5,8}\.(?i:doc|docx|xls|xlsx|ppt|pptx|hwp|hwpx)$"
)
_TEMP_NAME_ALLOWLIST = frozenset({
    "README", "LICENSE", "NOTICE", "INDEX", "CHANGES",
})

from gdrive_sync.utils import (
    matches_any, md5_file, mtime_to_iso, to_long_path,
)


log = logging.getLogger(__name__)


@dataclass
class LocalFile:
    """로컬 파일 한 항목."""
    rel_path: str                 # 루트 기준 상대 경로 (POSIX, '/')
    abs_path: Path
    size: int
    mtime_iso: str                # ISO 8601 Z
    is_folder: bool = False
    _md5: Optional[str] = None

    def md5(self) -> str:
        """MD5를 lazy 계산해 캐시."""
        if self.is_folder:
            return ""
        if self._md5 is None:
            self._md5 = md5_file(self.abs_path)
        return self._md5


class LocalScanner:
    """로컬 폴더 스캐너."""

    def __init__(
        self,
        root: Path,
        exclude_patterns: list[str],
        follow_symlinks: bool = False,
        stop_checker=None,           # Callable[[], bool] — True 시 스캔 중단
        progress_callback=None,      # Callable[[int], None] — 주기적 진행 콜백 (파일 수)
        heartbeat_sec: float = 1.5,  # 콜백 최소 간격 (초)
    ):
        self.root = root.resolve()
        self.exclude = exclude_patterns
        self.follow_symlinks = follow_symlinks
        self.stop_checker = stop_checker
        self.progress_callback = progress_callback
        self.heartbeat_sec = heartbeat_sec

    def scan(self) -> dict[str, LocalFile]:
        """루트 하위 전체를 스캔해 { rel_path: LocalFile } 반환.

        stop_checker가 True 반환하면 중도 종료 (부분 결과 반환).
        progress_callback이 있으면 heartbeat_sec 간격으로 파일 수를 알린다.
        """
        import time as _time
        if not self.root.exists():
            log.info(f"로컬 루트가 없어 생성합니다: {self.root}")
            self.root.mkdir(parents=True, exist_ok=True)

        result: dict[str, LocalFile] = {}
        last_emit = _time.monotonic()
        for lf in self._walk():
            if self.stop_checker and self.stop_checker():
                log.warning(f"스캔 중단 요청 — {len(result)}개까지 스캔 완료")
                break
            result[lf.rel_path] = lf
            if self.progress_callback:
                now = _time.monotonic()
                if now - last_emit >= self.heartbeat_sec:
                    try:
                        self.progress_callback(len(result))
                    except Exception:
                        pass
                    last_emit = now
        # 스캔 완료 시점에 최종 카운트 1회 더 (작은 폴더도 확실히 알림)
        if self.progress_callback:
            try:
                self.progress_callback(len(result))
            except Exception:
                pass
        return result

    def _walk(self) -> Iterator[LocalFile]:
        """os.scandir 기반 트리 순회.

        os.walk 대비 이점:
        - Windows: scandir의 DirEntry가 stat 결과를 캐싱 → entry.stat()이 추가 syscall 없이 즉시 반환
        - 모든 OS: entry.is_dir()이 d_type 사용 → stat syscall 절약
        - Path() 객체 생성 횟수 감소
        """
        root_str = to_long_path(self.root)
        yield from self._scandir_recursive(root_str, "")

    def _scandir_recursive(self, dir_str: str, rel_prefix: str) -> Iterator[LocalFile]:
        try:
            it = os.scandir(dir_str)
        except PermissionError as e:
            log.debug(f"scandir 접근 거부 (건너뜀): {dir_str}: {e}")
            return
        except OSError as e:
            log.warning(f"scandir 실패 (건너뜀): {dir_str}: {e}")
            return

        with it:
            for entry in it:
                if self.stop_checker and self.stop_checker():
                    return

                name = entry.name
                # \\?\ 접두사 등 정리는 root_str 처리에서 끝났으므로 entry.path를 그대로 사용 가능
                rel = f"{rel_prefix}/{name}" if rel_prefix else name

                # 빠른 제외 (하드코딩)
                if name.startswith(".gdrive_sync_"):
                    continue
                if name.endswith(".gdrsync.part"):
                    continue
                if name.startswith("~$"):        # MS Office 잠금 파일 (~$document.docx)
                    continue
                if name.startswith(".~lock."):   # LibreOffice 잠금 파일 (.~lock.file.odt#)
                    continue
                if _RE_OFFICE_TEMP.match(name) and Path(name).stem.upper() not in _TEMP_NAME_ALLOWLIST:
                    continue    # Word/한글/백신 임시파일 추정 (LHZ5DWIU.DOCX, JRZ5L.DOCX 등)

                # 패턴 제외
                if matches_any(rel, self.exclude) or matches_any(name, self.exclude):
                    continue

                # 심볼릭 링크는 업로드/다운로드 대상이 아니므로 스킵
                # (iCloud Drive의 Desktop·Documents 등이 symlink인 경우 IsADirectoryError 방지)
                if entry.is_symlink():
                    log.debug(f"심볼릭 링크 스킵: {entry.path}")
                    continue

                # Windows NTFS 정션(reparse point) 스킵
                # My Music / My Pictures / My Videos 등 시스템 호환용 정션은 접근 거부 ACL이 걸려 있음
                if sys.platform == "win32":
                    try:
                        fa = entry.stat(follow_symlinks=False).st_file_attributes
                        if fa & _stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            log.debug(f"NTFS 리파스 포인트 스킵: {entry.path}")
                            continue
                    except OSError:
                        pass

                # 디렉터리 vs 파일 판정 (d_type 캐시 사용 → stat 호출 안 함)
                try:
                    is_dir = entry.is_dir(follow_symlinks=self.follow_symlinks)
                except OSError:
                    continue

                if is_dir:
                    if name == ".gdrive_sync_trash":
                        continue
                    yield from self._scandir_recursive(entry.path, rel)
                    continue

                # 파일: stat (Windows는 캐시 사용해 syscall 절약)
                try:
                    st = entry.stat(follow_symlinks=self.follow_symlinks)
                except (OSError, FileNotFoundError):
                    log.debug(f"stat 실패 (건너뜀): {entry.path}")
                    continue

                yield LocalFile(
                    rel_path=rel,
                    abs_path=Path(entry.path),
                    size=st.st_size,
                    mtime_iso=mtime_to_iso(st.st_mtime),
                    is_folder=False,
                )


def is_file_locked(path: Path) -> bool:
    """파일이 잠금 상태(다른 프로세스가 독점 중)인지 확인.

    Windows: 열어보고 실패하면 잠김. Unix: 대부분 공유 모드이므로 False.
    """
    import sys
    if sys.platform != "win32":
        return False
    try:
        # rb+ 모드로 잠깐 열었다가 닫기 — 잠긴 파일은 여기서 실패
        with open(path, "rb+"):
            pass
        return False
    except (PermissionError, OSError):
        return True
