"""동기화 상태 파일(.gdrive_sync_state.json) 관리.

3-way 동기화의 '이전 상태'(snapshot)를 기록/조회.
원자적 쓰기 (임시 파일 → rename)로 중단에도 안전.

크로스플랫폼 숨김 처리:
- macOS/Linux: .으로 시작하는 파일은 자동으로 숨김 (OS 기본 동작)
- Windows:     파일 속성에 HIDDEN 플래그 설정 (ctypes로 Win32 API 호출)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class FileState:
    """마지막 동기화 시점의 파일 상태 한 항목."""
    # 로컬 정보
    local_size: Optional[int] = None
    local_mtime: Optional[str] = None   # ISO 8601 Z
    local_md5: Optional[str] = None

    # 리모트 정보
    remote_id: Optional[str] = None     # Google Drive file ID
    remote_size: Optional[int] = None
    remote_mtime: Optional[str] = None  # ISO 8601 Z
    remote_md5: Optional[str] = None

    # 메타 (필요 시)
    mime_type: Optional[str] = None     # Drive mimeType
    is_folder: bool = False


@dataclass
class SyncState:
    """특정 sync_pair 하나에 대한 상태 전체."""
    last_sync: Optional[str] = None
    # key: 로컬 루트 기준 상대 경로 (POSIX, '/'구분)
    files: dict[str, FileState] = field(default_factory=dict)


def state_path_for(local_root: Path) -> Path:
    """로컬 루트 아래 .gdrive_sync_state.json 경로."""
    return local_root / ".gdrive_sync_state.json"


def load_state(local_root: Path) -> SyncState:
    """상태 파일 로드. 없거나 손상 시 빈 상태 반환."""
    sp = state_path_for(local_root)
    if not sp.exists():
        return SyncState()
    try:
        with open(sp, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return SyncState()

    files: dict[str, FileState] = {}
    for rel, data in (raw.get("files") or {}).items():
        files[rel] = FileState(**{
            k: data.get(k) for k in (
                "local_size", "local_mtime", "local_md5",
                "remote_id", "remote_size", "remote_mtime", "remote_md5",
                "mime_type",
            )
        } | {"is_folder": bool(data.get("is_folder", False))})
    return SyncState(last_sync=raw.get("last_sync"), files=files)


def save_state(local_root: Path, state: SyncState) -> None:
    """상태 파일을 원자적으로 저장.

    임시 파일에 먼저 쓰고 os.replace로 교체 → 중단 시에도 원본 보존.
    """
    sp = state_path_for(local_root)
    sp.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "last_sync": state.last_sync,
        "files": {rel: asdict(fs) for rel, fs in state.files.items()},
    }

    # 같은 볼륨에 임시 파일 생성 (cross-device rename 방지)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".gdrive_sync_state_",
        suffix=".tmp",
        dir=str(sp.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, sp)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    # 크로스플랫폼 숨김 처리
    set_hidden(sp)


class StateWriter:
    """state.json 비동기(백그라운드) 쓰기 (D).

    sync_pair 종료마다 수십 MB JSON 직렬화·디스크 쓰기로 다음 폴더가
    멈춰 있던 문제를 단일 워커 스레드로 비동기화.

    사용 패턴:
        writer = StateWriter()
        writer.start()
        try:
            writer.enqueue(path, state)
            ...
        finally:
            writer.shutdown(wait=True)   # 모든 대기 항목 flush 후 종료

    중요:
    - shutdown(wait=True) 가 호출 안 되면 큐에 남은 state 가 손실됨
      (디스크 안전성: 손실돼도 데이터 자체는 안 사라짐 — 다음 sync 에서 재검토).
    - enqueue 후 state 객체를 수정하지 말 것 — writer 가 그대로 직렬화함.
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: "queue.Queue[object]" = queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._last_error: Optional[Exception] = None

    def start(self) -> None:
        if self._started:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gdrv-state-writer",
        )
        self._started = True
        self._thread.start()

    def enqueue(self, local_root: Path, state: SyncState) -> None:
        if not self._started:
            # 시작 안 됐으면 동기 fallback
            save_state(local_root, state)
            return
        self._q.put((local_root, state))

    def flush(self) -> None:
        """현재 큐에 들어 있는 모든 항목이 처리될 때까지 블록."""
        if self._started:
            self._q.join()

    def shutdown(self, wait: bool = True) -> None:
        if not self._started:
            return
        self._q.put(self._SENTINEL)
        if wait and self._thread:
            self._thread.join()
        self._started = False
        self._thread = None

    @property
    def last_error(self) -> Optional[Exception]:
        return self._last_error

    def _run(self) -> None:
        while True:
            item = self._q.get()
            try:
                if item is self._SENTINEL:
                    return
                local_root, state = item   # type: ignore[misc]
                try:
                    save_state(local_root, state)
                except Exception as e:
                    log.error(f"state 비동기 저장 실패 {local_root}: {e}")
                    self._last_error = e
            finally:
                self._q.task_done()


def clear_state(local_root: Path) -> bool:
    """상태 파일 삭제. reset-state 명령용."""
    sp = state_path_for(local_root)
    if sp.exists():
        sp.unlink()
        return True
    return False


# ──────────────────────────────────────────────────────────
# 크로스플랫폼 숨김 처리
# ──────────────────────────────────────────────────────────

def set_hidden(path: Path) -> None:
    """파일/폴더를 OS별 숨김 처리.

    - Windows: FILE_ATTRIBUTE_HIDDEN (0x02) 설정 (Win32 API)
    - macOS/Linux: .으로 시작하면 자동 숨김 (추가 작업 불필요)
    """
    if sys.platform != "win32":
        return  # Unix는 .파일이 자동 숨김
    try:
        import ctypes
        # FILE_ATTRIBUTE_HIDDEN = 0x02
        # 기존 속성을 읽고 HIDDEN 비트만 추가 (다른 속성 보존)
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == -1:  # INVALID_FILE_ATTRIBUTES
            return
        if not (attrs & 0x02):  # HIDDEN 아직 안 되어 있으면
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs | 0x02)
    except Exception as e:
        log.debug(f"숨김 속성 설정 실패 (무시): {path}: {e}")
