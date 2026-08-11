"""3-way 양방향 동기화 엔진 (v2: 병렬 전송 + 대역폭 제한).

이전 상태(state) ↔ 현재 로컬 ↔ 현재 리모트를 비교해 9가지 시나리오별로 액션 결정.

v2 변경점:
- 업/다운로드는 TransferPool을 통한 병렬 실행
- 삭제/충돌(keep_both)/상태삭제 등은 메인 스레드에서 순차 실행 (안전)
- 병렬 전에 필요한 상위 폴더를 사전 생성 (_path_cache 채우기 → 경쟁 방지)
- state.files 업데이트는 메인 스레드에서 일괄 반영 (race 없음)

변경 감지 기준:
- size / mtime (RFC3339 UTC 비교) / md5 (Drive md5Checksum ↔ 로컬)
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from gdrive_sync.bandwidth import BandwidthLimiter
from gdrive_sync.conflict import ConflictResolution, resolve as resolve_conflict
from gdrive_sync.config import Config, SyncPair
from gdrive_sync.drive_api import DriveClient, DriveFile, FOLDER_MIME
from gdrive_sync.local_scanner import LocalFile, LocalScanner, is_file_locked
from gdrive_sync.state import (
    FileState, StateWriter, SyncState, load_state, save_state,
)
from gdrive_sync.transfer_pool import (
    DownloadTask, TransferPool, TransferResult, UploadTask,
)
from gdrive_sync.utils import (
    human_size, mtime_close, mtime_to_iso, parse_rfc3339, to_posix, utcnow_iso,
)


log = logging.getLogger(__name__)

# OS 가 자동 생성하는 폴더 메타데이터 — 이것만 남은 폴더는 '빈 폴더'로 간주해 함께 제거.
# (config 기본 제외 패턴의 .DS_Store/Thumbs.db/desktop.ini 와 동일 집합)
_PRUNABLE_JUNK = frozenset({".DS_Store", "Thumbs.db", "desktop.ini"})


def sweep_empty_dirs(
    root: Path,
    apply: bool = False,
    max_depth: int = 20,
    junk: frozenset = _PRUNABLE_JUNK,
) -> list[Path]:
    """`root` 하위의 빈 폴더(또는 OS junk 만 남은 폴더)를 bottom-up 으로 일괄 제거.

    동기화 중 자동 정리(`_prune_empty_dirs`)는 '새 삭제가 일어난 경로'만 위로
    훑지만, 이 함수는 이미 비어버린 폴더까지 트리 전체를 한 번에 청소한다
    (`clean-empty-folders --local` 용). 순수 pathlib/os → 크로스플랫폼.

    안전망:
    - `root` 자체는 절대 제거하지 않음.
    - junk 외 실제 파일/하위폴더가 하나라도 있으면 그 폴더는 보존.
    - 깊은 폴더부터 처리 → 자식이 비면 부모도 같은 패스에서 비는 것으로 판정
      (dry-run 도 `removed` 집합으로 동일하게 계단식 시뮬레이션).

    Args:
        apply: False(기본)면 제거하지 않고 대상만 반환(dry-run).
    Returns:
        제거된(또는 dry-run 시 제거 대상) 폴더의 `root` 기준 상대경로 리스트.
    """
    try:
        root = root.resolve()
    except Exception:
        return []
    if not root.is_dir():
        return []

    # 1) max_depth 이내 모든 하위 폴더 수집 (root 제외)
    all_dirs: list[Path] = []
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        try:
            depth = len(d.relative_to(root).parts)
        except Exception:
            continue
        if depth == 0:
            continue
        if depth >= max_depth:
            dirnames[:] = []  # 더 깊이 내려가지 않음
        all_dirs.append(d)

    # 2) 깊이 내림차순 — 자식부터 처리
    all_dirs.sort(key=lambda p: len(p.parts), reverse=True)

    removed: list[Path] = []
    removed_set: set[Path] = set()
    for d in all_dirs:
        if not d.is_dir():
            continue
        try:
            entries = list(d.iterdir())
        except Exception:
            continue
        # 이미 제거(예정)된 자식 + junk 는 비어있는 것으로 간주
        leftover = [
            e for e in entries
            if e.name not in junk and e not in removed_set
        ]
        if leftover:
            continue
        if apply:
            ok = True
            for e in entries:
                if e in removed_set:
                    continue  # 이미 rmdir 된 자식
                try:
                    e.unlink()  # junk 제거
                except Exception:
                    ok = False
                    break
            if not ok:
                continue
            try:
                d.rmdir()
            except OSError:
                continue
        removed.append(d.relative_to(root))
        removed_set.add(d)
    return removed


# ──────────────────────────────────────────────────────────
# 마운트/접근 가능성 체크
# ──────────────────────────────────────────────────────────

def check_local_path_accessible(local_path: Path) -> Optional[str]:
    """sync_pair 실행 전 로컬 경로가 접근 가능한지 확인.

    Returns:
        None: 접근 가능 (또는 생성 가능)
        str:  사용자에게 보여줄 경고 메시지 (마운트 없음 등)
    """
    # 이미 존재하면 OK
    if local_path.exists():
        try:
            # 실제 들여다볼 수 있는지 (암호화·잠김 드라이브 대응)
            next(iter(local_path.iterdir()), None)
        except PermissionError:
            return f"폴더에 접근할 수 없습니다 (권한 거부). 잠긴 드라이브를 잠금 해제해 주세요: {local_path}"
        except OSError as e:
            return f"폴더 접근 실패 ({e}): {local_path}"
        return None

    # 없으면 위로 올라가며 존재하는 조상 찾기
    ancestor = local_path.parent
    while ancestor != ancestor.parent and not ancestor.exists():
        ancestor = ancestor.parent

    # macOS: /Volumes 바로 밑 (외장 드라이브 미마운트)
    if str(ancestor) == "/Volumes":
        try:
            mount_name = local_path.relative_to("/Volumes").parts[0]
            return (
                f"외장 드라이브 '{mount_name}'이(가) 마운트되지 않았습니다. "
                f"드라이브를 연결한 뒤 다시 시도하세요."
            )
        except (ValueError, IndexError):
            pass

    # Linux: /mnt 또는 /media 바로 밑
    for base in ("/mnt", "/media"):
        if str(ancestor) == base:
            try:
                mount_name = local_path.relative_to(base).parts[0]
                return (
                    f"마운트 '{base}/{mount_name}'이(가) 없습니다. "
                    f"장치를 연결한 뒤 다시 시도하세요."
                )
            except (ValueError, IndexError):
                pass

    # Windows: 드라이브 루트 체크 (예: E:\... 인데 E: 없음)
    parts = local_path.parts
    if parts and len(parts[0]) >= 2 and parts[0][1] == ":":
        drive_root = Path(parts[0] + "\\")
        if not drive_root.exists():
            return (
                f"드라이브 '{parts[0]}'에 접근할 수 없습니다. "
                f"연결 상태를 확인하세요."
            )

    # 부모 조상이 존재하고 쓰기 가능하면 scanner가 mkdir로 만들 수 있음
    return None


# ──────────────────────────────────────────────────────────
# 액션 정의
# ──────────────────────────────────────────────────────────

class ActionType(str, Enum):
    UPLOAD_NEW = "upload_new"              # 로컬에만 있음 → 업로드
    UPLOAD_UPDATE = "upload_update"        # 로컬 변경 → 업로드 덮어쓰기
    DOWNLOAD_NEW = "download_new"          # 리모트에만 있음 → 다운로드
    DOWNLOAD_UPDATE = "download_update"    # 리모트 변경 → 다운로드 덮어쓰기
    DELETE_LOCAL = "delete_local"          # 리모트에서 삭제 → 로컬 삭제
    DELETE_REMOTE = "delete_remote"        # 로컬에서 삭제 → 리모트 삭제
    MOVE_REMOTE = "move_remote"            # 로컬 rename/이동 감지 → Drive 서버측 이동
    MOVE_LOCAL = "move_local"              # 리모트 rename/이동 감지 → 로컬 파일 이동
    CONFLICT_UPLOAD = "conflict_upload"
    CONFLICT_DOWNLOAD = "conflict_download"
    CONFLICT_KEEP_BOTH = "conflict_keep_both"
    SKIP_SAME = "skip_same"                # 동일 (MD5 일치 등)
    SKIP_LOCKED = "skip_locked"            # 파일 잠김
    SKIP_WORKSPACE = "skip_workspace"      # Google Docs 등 네이티브
    REMOVE_STATE = "remove_state"          # 양쪽 삭제됨


_UPLOAD_TYPES = {
    ActionType.UPLOAD_NEW,
    ActionType.UPLOAD_UPDATE,
    ActionType.CONFLICT_UPLOAD,
}
_DOWNLOAD_TYPES = {
    ActionType.DOWNLOAD_NEW,
    ActionType.DOWNLOAD_UPDATE,
    ActionType.CONFLICT_DOWNLOAD,
}
_PARALLEL_TYPES = _UPLOAD_TYPES | _DOWNLOAD_TYPES


@dataclass
class Action:
    type: ActionType
    rel_path: str
    local: Optional[LocalFile] = None
    remote: Optional[DriveFile] = None
    prior: Optional[FileState] = None
    rename_to: Optional[str] = None
    # MOVE_REMOTE/MOVE_LOCAL 전용 — 이동 전 상대경로 (rel_path 는 이동 후 경로)
    move_from: Optional[str] = None


@dataclass
class SyncSummary:
    uploaded: int = 0
    uploaded_bytes: int = 0
    downloaded: int = 0
    downloaded_bytes: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    conflicts: int = 0
    skipped: int = 0
    errors: int = 0
    # 스캔 후 업로드 직전에 사라진 파일 수 (백신/DLP 임시파일 등)
    # 진짜 오류가 아니므로 errors와 별도 카운트
    vanished: int = 0
    # 사라진 파일들의 basename 빈도 (요약에 Top 3 표시용)
    vanished_samples: dict[str, int] = field(default_factory=dict)
    # 양쪽 모두 사라져 state 만 조용히 정리한 항목 수 (REMOVE_STATE).
    # 가시성 없으면 "핑퐁성 누락" 같은 이상 패턴을 운영자가 절대 못 알아챔.
    removed_state: int = 0
    # 그 중 가장 앞 몇 개 샘플 (로그/요약에서 Top N 노출)
    removed_state_samples: list[str] = field(default_factory=list)
    # 파일 삭제로 비게 된 후 자동 제거한 로컬 빈 폴더 수 (크로스플랫폼)
    pruned_dirs: int = 0
    # rename/이동 감지로 재전송 대신 이동 처리한 건수
    moved_remote: int = 0   # 로컬 rename → Drive 서버측 이동
    moved_local: int = 0    # Drive rename → 로컬 파일 이동
    # Drive 쪽 NFD 파일명을 서버측 rename 으로 NFC 정규화한 건수
    normalized_remote: int = 0
    # 파일 삭제/이동으로 비게 된 후 자동 trash 한 Drive 빈 폴더 수
    pruned_remote_dirs: int = 0
    actions: list[Action] = field(default_factory=list)


def format_vanished_samples(samples: dict[str, int], top_n: int = 3) -> str:
    """사라진 파일 빈도를 'a.docx (15회), b.docx (2회)' 형식으로 변환.

    빈 딕셔너리면 빈 문자열 반환. top_n 초과분은 '...외 N건'으로 묶음.
    """
    if not samples:
        return ""
    sorted_items = sorted(samples.items(), key=lambda kv: -kv[1])
    head = sorted_items[:top_n]
    parts = [f"{name} ({cnt}회)" for name, cnt in head]
    rest = len(sorted_items) - len(head)
    if rest > 0:
        parts.append(f"...외 {rest}종")
    return ", ".join(parts)


# ──────────────────────────────────────────────────────────
# 메인 엔진
# ──────────────────────────────────────────────────────────

class SyncEngine:
    def __init__(
        self,
        cfg: Config,
        drive: DriveClient,
        dry_run: bool = False,
        force_mode: Optional[str] = None,       # None | "upload" | "download"
        progress_factory: Optional[Callable] = None,
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
        parallel_override: Optional[int] = None,  # CLI 오버라이드
        progress_callback: Optional[Callable] = None,  # (completed, total, rel_path)
        status_callback: Optional[Callable] = None,    # (phase_text: str) — 단계 변화 알림
        progress_tracker=None,                         # ProgressTracker 인스턴스 (전체 진행 추적, 옵션)
    ):
        self.cfg = cfg
        self.drive = drive
        self.dry_run = dry_run
        self.force_mode = force_mode
        self.progress_factory = progress_factory
        self.bandwidth = bandwidth_limiter
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.progress_tracker = progress_tracker

        # CLI --parallel N 이 있으면 덮어쓰기
        if parallel_override is not None:
            self.cfg.performance.parallel_transfers = max(1, min(10, parallel_override))

        # state 업데이트 보호 (메인 스레드에서만 쓰지만 future-safe)
        self._state_lock = threading.Lock()

        # 이번 pair 에서 파일이 삭제/이동돼 비었을 가능성이 있는 Drive 폴더
        # (pair-상대 POSIX 경로). 실행 후 _prune_empty_remote_dirs 가 정리.
        self._remote_prune_rels: set[str] = set()

        # 3단계 중단 플래그 (강도 순)
        # Level 1 (가장 약함): 현재 전송 중인 파일까지만 끝내고 더 이상 새 파일/폴더 시작 안 함
        # Level 2: 현재 폴더 끝까지 (모든 파일) → 다음 폴더 안 시작
        # Level 3 (가장 강함): 즉시 워커 죽임 (네트워크 read/write 중간이라도 끊음)
        # 상위 단계는 하위 포함 — 즉, force는 file/pair stop도 트리거
        self._stop_after_file = threading.Event()
        self._stop_after_pair = threading.Event()
        self._force_stop = threading.Event()
        self._current_pool = None   # TransferPool 인스턴스 (병렬 전송 시작 후)

        # state.json 비동기 writer (D) — run() 진입 시 start, finally 시 shutdown
        self._state_writer: Optional[StateWriter] = None

    # ──────────────────────────────────────────────
    # 중단 요청 (3단계)
    # ──────────────────────────────────────────────
    def request_stop_after_file(self) -> None:
        """현재 전송 중인 파일까지만 완료. 새 파일 큐에 안 넣음.

        다음 폴더로도 안 넘어감 (자동으로 pair-level stop도 작동).
        """
        self._stop_after_file.set()
        # 진행 중인 파일은 그냥 끝내라고 풀에 알림 (graceful)
        if self._current_pool:
            self._current_pool.request_stop()

    def request_stop_after_pair(self) -> None:
        """현재 폴더의 모든 파일 끝까지 완료. 다음 폴더 안 시작.

        가장 안전한 중단 — 폴더 단위 일관성 보장.
        """
        self._stop_after_pair.set()
        # 풀에는 알리지 않음 — 현재 폴더는 끝까지 진행

    def request_force_stop(self) -> None:
        """즉시 모든 작업 중단. 네트워크 read/write 도중이라도 끊음."""
        self._force_stop.set()
        self._stop_after_pair.set()    # 상위 단계 자동 트리거
        self._stop_after_file.set()
        if self._current_pool:
            self._current_pool.request_stop()

    # ──────────────────────────────────────────────
    # 호환용 — 기존 코드/테스트가 사용하는 API
    # ──────────────────────────────────────────────
    def request_stop(self) -> None:
        """기존 [중단] 동작 = 파일 단위 graceful stop."""
        self.request_stop_after_file()

    def _set_status(self, phase_text: str) -> None:
        """단계(phase) 변화를 GUI에 알림. 콜백 없거나 실패해도 엔진은 계속."""
        if self.status_callback:
            try:
                self.status_callback(phase_text)
            except Exception:
                pass

    # ──────────────────────────────────────────────
    # 중단 상태 조회
    # ──────────────────────────────────────────────
    def is_stop_after_file_requested(self) -> bool:
        """파일 단위 또는 그 이상의 중단이 요청됐는지."""
        return (
            self._stop_after_file.is_set()
            or self._stop_after_pair.is_set()
            or self._force_stop.is_set()
        )

    def is_stop_after_pair_requested(self) -> bool:
        """폴더 단위 또는 그 이상의 중단이 요청됐는지."""
        return self._stop_after_pair.is_set() or self._force_stop.is_set()

    def is_force_stop_requested(self) -> bool:
        return self._force_stop.is_set()

    def is_stop_requested(self) -> bool:
        """호환용. file-level 이상이면 True."""
        return self.is_stop_after_file_requested()

    # ──────────────────────────────────────────────
    # 전체 실행
    # ──────────────────────────────────────────────
    def run(self) -> list[tuple[SyncPair, SyncSummary]]:
        results: list[tuple[SyncPair, SyncSummary]] = []

        # D) state.json 비동기 writer 시작 — finally 에서 반드시 shutdown
        self._state_writer = StateWriter()
        self._state_writer.start()

        try:
            for pair in self.cfg.sync_pairs:
                # 다음 pair 시작 전에 폴더-단위 중단 체크
                # (파일-단위 중단도 폴더 사이엔 동일 효과 — 새 폴더 안 시작)
                if self.is_stop_after_pair_requested() or self._stop_after_file.is_set():
                    log.warning(
                        f"중단 요청 — 남은 폴더 {len(self.cfg.sync_pairs) - len(results)}개 스킵"
                    )
                    break
                log.info(f"=== {pair.local_path} ↔ {pair.remote_path} ===")
                summary = self.sync_pair(pair)
                results.append((pair, summary))

            # 휴지통 자동 정리 (dry-run 제외, 동기화 후에만)
            if not self.dry_run and self.cfg.trash.auto_cleanup_days > 0:
                try:
                    from gdrive_sync.cleanup import cleanup_old_trash
                    n, b = cleanup_old_trash(self.cfg.trash.auto_cleanup_days)
                    if n > 0:
                        log.info(
                            f"휴지통 자동 정리: {n}개 파일 삭제 "
                            f"({b / 1024 / 1024:.1f} MB) — "
                            f"{self.cfg.trash.auto_cleanup_days}일 이상 지난 파일"
                        )
                except Exception as e:
                    log.warning(f"휴지통 자동 정리 실패: {e}")
        finally:
            # 큐에 쌓인 state 모두 디스크에 쓴 뒤 종료 — 정상/예외 모두 보장
            try:
                self._state_writer.shutdown(wait=True)
            except Exception as e:
                log.error(f"StateWriter shutdown 실패: {e}")
            self._state_writer = None

        return results

    def sync_pair(self, pair: SyncPair) -> SyncSummary:
        summary = SyncSummary()

        # ProgressTracker: pair 시작 알림
        if self.progress_tracker:
            try:
                self.progress_tracker.on_pair_start(pair)
            except Exception:
                pass

        # 0) 마운트/접근 가능성 사전 체크
        mount_err = check_local_path_accessible(pair.local_path)
        if mount_err:
            log.warning(f"⚠ 스킵: {pair.local_path} — {mount_err}")
            summary.errors = 0   # 진짜 오류는 아님 (단순 스킵)
            summary.skipped = 1
            if self.progress_tracker:
                try:
                    # 스킵된 pair는 분모에서 제거 (실제 0개)
                    self.progress_tracker.on_pair_scanned(pair, files=0, bytes_total=0)
                    self.progress_tracker.on_pair_done()
                except Exception:
                    pass
            return summary

        pair_label = pair.local_path.name or str(pair.local_path)

        # 0.5) 옵션: 분절 한글 파일명 NFC 정규화 (macOS↔Windows 호환)
        if getattr(self.cfg, "auto_normalize_filenames", False):
            try:
                from gdrive_sync.normalize import normalize_path
                self._set_status(f"🔤 파일명 정규화 중 · {pair_label}")
                rep = normalize_path(pair.local_path, dry_run=False)
                if rep.renamed > 0 or rep.deduped > 0:
                    parts = [f"변경 {rep.renamed}개"] if rep.renamed else []
                    if rep.deduped:
                        parts.append(f"NFD중복삭제 {rep.deduped}개")
                    log.info(
                        f"파일명 정규화: {', '.join(parts)} "
                        f"(검사 {rep.scanned}, 충돌 {rep.skipped_conflict}, "
                        f"오류 {rep.errors})"
                    )
                elif rep.errors > 0:
                    log.warning(f"파일명 정규화 중 오류 {rep.errors}건 발생")
            except Exception as e:
                log.warning(f"파일명 정규화 실패(스킵): {e}")

        # 1+2+3) 로컬 스캔 + Drive 트리 스캔을 병렬로
        # 두 작업은 완전히 독립적이라 동시 실행 시 전체 시간 = max(local, remote)
        # 로컬은 디스크 I/O, Drive는 네트워크 I/O라 자원 경합도 거의 없음
        import time as _time
        from concurrent.futures import ThreadPoolExecutor

        self._set_status(f"⏳ 스캔 중 (로컬 + Drive 병렬) · {pair_label}")

        def _local_heartbeat(count: int) -> None:
            self._set_status(f"📂 로컬 스캔 · {pair_label} · {count:,}개")

        scanner = LocalScanner(
            pair.local_path,
            self.cfg.exclude_patterns,
            stop_checker=self.is_force_stop_requested,  # 강제 중단 시에만 스캔 중단
            progress_callback=_local_heartbeat,
        )

        def _do_local_scan() -> dict[str, LocalFile]:
            return scanner.scan()

        def _do_remote_scan() -> tuple[str, dict[str, DriveFile], list[str], list]:
            self._set_status(f"☁ Drive 폴더 조회 · {pair.remote_path}")
            rid = self.drive.resolve_folder_path(
                pair.remote_path, create_missing=True,
            )
            rfiles: dict[str, DriveFile] = {}
            rfolders: list[tuple[str, DriveFile]] = []
            wskipped: list[str] = []
            last_emit = _time.monotonic()
            scanned = 0
            for rel, df in self.drive.list_tree(rid):
                scanned += 1
                now = _time.monotonic()
                if now - last_emit >= 1.5:
                    self._set_status(
                        f"☁ Drive 조회 · {pair.remote_path} · {scanned:,}개"
                    )
                    last_emit = now
                if df.is_folder:
                    rfolders.append((rel, df))
                    continue
                if df.is_workspace_native:
                    wskipped.append(rel)
                    continue
                rfiles[rel] = df
            return rid, rfiles, wskipped, rfolders

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gdrv-scan") as _scan_ex:
            fut_local = _scan_ex.submit(_do_local_scan)
            fut_remote = _scan_ex.submit(_do_remote_scan)
            # 두 작업 모두 완료 대기 (예외도 여기서 전파)
            local_files: dict[str, LocalFile] = fut_local.result()
            remote_root_id, remote_files, workspace_skipped, remote_folders = (
                fut_remote.result()
            )

        # 3.5) 옵션: Drive 쪽 NFD 파일명 정규화 (맥에서 직접 업로드된 분절 한글).
        #      서버측 rename 이라 재전송 없음. NFD 가 로컬로 내려오기 전에 원천 차단.
        if getattr(self.cfg, "auto_normalize_filenames", False):
            try:
                remote_files = self._normalize_remote_names(
                    pair, remote_folders, remote_files, summary,
                )
            except Exception as e:
                log.warning(f"Drive 파일명 정규화 실패(스킵): {e}")

        log.info(f"로컬 파일: {len(local_files)}개")
        log.info(f"리모트 파일: {len(remote_files)}개")
        if workspace_skipped:
            log.info(
                f"Google Workspace 네이티브 파일 {len(workspace_skipped)}개 제외 "
                f"(예: {workspace_skipped[:3]}...)"
            )

        # 강제 중단 시에만 폴더 중간에서 빠져나옴
        # (파일/폴더 단위 중단은 현재 폴더는 끝까지 진행)
        if self.is_force_stop_requested():
            log.warning("강제 중단 — 현재 폴더 중단")
            return summary

        # ProgressTracker: 스캔 완료 → 분모 보정
        # (로컬 + 리모트 합집합이 이번 pair에서 처리 대상이 될 잠재 파일 수)
        if self.progress_tracker:
            try:
                all_paths = set(local_files.keys()) | set(remote_files.keys())
                pair_files = len(all_paths)
                pair_bytes = sum(lf.size for lf in local_files.values())
                self.progress_tracker.on_pair_scanned(pair, files=pair_files, bytes_total=pair_bytes)
            except Exception:
                pass

        # 4) 이전 상태 로드
        state = load_state(pair.local_path)

        # 5) 액션 결정
        self._set_status(f"↕ 변경사항 분석 중 · {pair_label}")
        actions = self._decide_actions(local_files, remote_files, state)
        summary.actions = actions

        # 6) 드라이런이면 여기서 종료
        if self.dry_run:
            self._log_dryrun_summary(actions)
            return summary

        # 7) 실행
        self._set_status(f"↑↓ 파일 전송 중 · {pair_label}")
        self._remote_prune_rels = set()
        self._execute(pair, actions, remote_root_id, state, summary)

        # 7.2) 파일 삭제/이동으로 비게 된 Drive 폴더 정리
        #      (로컬의 _prune_empty_dirs 와 대칭 — 폴더 rename 잔재 방지)
        try:
            self._prune_empty_remote_dirs(pair, summary)
        except Exception as e:
            log.warning(f"Drive 빈 폴더 정리 실패(스킵): {e}")

        # 7.5) REMOVE_STATE 대량 발생 시 경고 — Drive list_tree 일시 누락 + 로컬 정리가
        # 겹쳐 "양쪽 사라짐"으로 잘못 판정한 ghost-cleanup 의심. 진단 키워드 제공.
        if summary.removed_state > 0:
            # 임계: 20개 절대값 또는 prior state 의 5% 이상이면 WARN.
            threshold_abs = 20
            prior_total = len(state.files) + summary.removed_state  # 이번 sync 전 추정
            threshold_rel = max(1, prior_total // 20)  # 5%
            is_bulk = (
                summary.removed_state >= threshold_abs
                or summary.removed_state >= threshold_rel
            )
            level = log.warning if is_bulk else log.info
            samples = summary.removed_state_samples[:5]
            samples_str = ", ".join(samples)
            if len(summary.removed_state_samples) > 5:
                samples_str += f", … (+{summary.removed_state - 5}개)"
            tag = "⚠ 대량 ghost-cleanup 의심" if is_bulk else "ℹ"
            level(
                f"{tag}: '{pair_label}' 에서 REMOVE_STATE {summary.removed_state}개 "
                f"(이전 state 대비 {summary.removed_state * 100 / max(1, prior_total):.1f}%) — "
                f"샘플: {samples_str}"
            )
            if is_bulk:
                level(
                    "  ↳ 원인 가능성: (1) 사용자가 양쪽에서 의도적으로 삭제 "
                    "(2) Drive list_tree 일시 누락 + 로컬 정리 겹침 "
                    "(3) 다른 PC 가 먼저 휴지통 이동. "
                    "'gdrive-sync diagnose <폴더명>' 으로 Drive 상태 확인 가능."
                )

        # 8) 상태 저장 — writer 있으면 비동기(D), 없으면 동기 fallback
        state.last_sync = utcnow_iso()
        if self._state_writer is not None:
            self._state_writer.enqueue(pair.local_path, state)
        else:
            save_state(pair.local_path, state)

        # ProgressTracker: pair 완료
        if self.progress_tracker:
            try:
                self.progress_tracker.on_pair_done()
            except Exception:
                pass

        return summary

    # ──────────────────────────────────────────────
    # Drive 쪽 NFD 파일명 정규화 (스캔 직후)
    # ──────────────────────────────────────────────
    def _normalize_remote_names(
        self,
        pair: SyncPair,
        rfolders: list,
        rfiles: dict[str, DriveFile],
        summary: SyncSummary,
    ) -> dict[str, DriveFile]:
        """스캔 결과의 NFD 이름을 Drive 서버측 rename 으로 NFC 정규화하고
        rel 키를 재매핑한 rfiles 를 반환.

        맥에서 브라우저로 직접 업로드된 파일은 로컬 정규화(step 0.5)를 안
        거치므로 여기서 원천 차단. NFD 없으면 API 호출 0회 (사전 검사만).
        dry-run 은 건수만 보고하고 실제 rename 안 함.
        """
        from gdrive_sync.normalize import (
            is_decomposed, normalize_remote_entries, remap_remote_rel,
        )

        entries = list(rfolders) + list(rfiles.items())
        if not any(is_decomposed(df.name) for _, df in entries):
            return rfiles

        rep = normalize_remote_entries(self.drive, entries, dry_run=self.dry_run)
        if self.dry_run:
            log.info(
                f"[DRY-RUN] Drive NFD 이름 {rep.needs_fix}건 발견 "
                f"(정규화 예정 {rep.renamed}건, 충돌 {rep.skipped_conflict}건) "
                f"— 실제 sync 시 서버측 rename 으로 수정됨"
            )
            return rfiles

        log.info(
            f"🔤 Drive 파일명 정규화: {rep.renamed}건 완료"
            + (f", 충돌 스킵 {rep.skipped_conflict}건" if rep.skipped_conflict else "")
            + (f", 오류 {rep.errors}건" if rep.errors else "")
        )
        summary.normalized_remote += rep.renamed

        # rename 된 폴더의 path_cache 무효화 (옛 NFD 경로가 남지 않게)
        for old_rel in rep.renamed_folders:
            full = f"{pair.remote_path}/{old_rel}" if pair.remote_path else old_rel
            self.drive.invalidate_cached_path(full)

        if not (rep.renamed_files or rep.renamed_folders):
            return rfiles
        return {
            remap_remote_rel(rel, rep.renamed_folders, rep.renamed_files): df
            for rel, df in rfiles.items()
        }

    # ──────────────────────────────────────────────
    # 드라이런 요약 출력 (대량 파일 시 GUI 멈춤 방지)
    # ──────────────────────────────────────────────
    _DRYRUN_SAMPLE_SIZE = 30   # 파일명을 보여줄 최대 개수

    def _log_dryrun_summary(self, actions: list[Action]) -> None:
        """DRY-RUN 결과를 요약 출력.

        문제: 18,000개 파일을 한 줄씩 log.info 하면 Tkinter Text 위젯이
        렌더링에 밀려서 GUI가 "응답 없음" 상태에 빠짐.
        해결: 타입별 개수 요약 + 처음 N개만 샘플 표시.
        """
        if not actions:
            log.info("[DRY-RUN] 변경사항 없음")
            return

        # 타입별 개수 집계
        counts: dict[str, int] = {}
        for a in actions:
            counts[a.type.value] = counts.get(a.type.value, 0) + 1

        total = len(actions)
        sample = self._DRYRUN_SAMPLE_SIZE

        # 파일 수가 적으면 기존처럼 전부 표시
        if total <= sample * 2:
            for a in actions:
                log.info(f"[DRY-RUN] {a.type.value}: {a.rel_path}")
        else:
            # 비-skip 액션만 먼저 샘플 표시 (실제 변경 예정인 것 위주)
            non_skip = [a for a in actions if "skip" not in a.type.value]
            shown = 0
            for a in non_skip[:sample]:
                log.info(f"[DRY-RUN] {a.type.value}: {a.rel_path}")
                shown += 1
            remaining = len(non_skip) - shown
            if remaining > 0:
                log.info(f"[DRY-RUN] ... 외 {remaining}개 (비-skip 액션 총 {len(non_skip)}개)")
            if counts.get("skip_same", 0):
                log.info(f"[DRY-RUN] skip_same: {counts['skip_same']}개 (동일 파일, 전송 불필요)")

        # 타입별 요약 테이블
        log.info("[DRY-RUN] ── 요약 ──")
        for name, cnt in sorted(counts.items()):
            log.info(f"[DRY-RUN]   {name:<22} {cnt}개")
        log.info(f"[DRY-RUN]   {'합계':<22} {total}개")

    # ──────────────────────────────────────────────
    # 9가지 시나리오 판정 (기존과 동일)
    # ──────────────────────────────────────────────
    def _decide_actions(
        self,
        local: dict[str, LocalFile],
        remote: dict[str, DriveFile],
        state: SyncState,
    ) -> list[Action]:
        actions: list[Action] = []
        all_paths = set(local) | set(remote) | set(state.files)

        for rel in sorted(all_paths):
            lf = local.get(rel)
            rf = remote.get(rel)
            prior = state.files.get(rel)

            # 강제 모드 = 미러(mirror) 동기화.
            # a → b 완전 동일 상태를 목표로, 반대편에만 있는 파일은 삭제한다.
            # 삭제 정책은 config의 delete_policy를 따름 (기본 trash — 휴지통이라 복구 가능).
            #
            # 변경 없는 파일 재전송 방지: 양쪽이 이미 동일하면 SKIP_SAME.
            # _files_match()가 prior(state) 또는 MD5 기반으로 동일성 판정.
            if self.force_mode == "upload":
                if lf and rf and self._files_match(lf, rf, prior):
                    actions.append(Action(ActionType.SKIP_SAME, rel, lf, rf, prior))
                elif lf:
                    act = ActionType.UPLOAD_UPDATE if rf else ActionType.UPLOAD_NEW
                    actions.append(Action(act, rel, lf, rf, prior))
                elif rf:
                    # Drive에만 있는 파일 → 로컬 미러를 위해 Drive에서 삭제 (휴지통)
                    actions.append(Action(ActionType.DELETE_REMOTE, rel, None, rf, prior))
                # lf도 rf도 없으면 prior만 남았어도 처리 불필요
                continue
            if self.force_mode == "download":
                if lf and rf and self._files_match(lf, rf, prior):
                    actions.append(Action(ActionType.SKIP_SAME, rel, lf, rf, prior))
                elif rf:
                    act = ActionType.DOWNLOAD_UPDATE if lf else ActionType.DOWNLOAD_NEW
                    actions.append(Action(act, rel, lf, rf, prior))
                elif lf:
                    # 로컬에만 있는 파일 → Drive 미러를 위해 로컬에서 삭제 (휴지통)
                    actions.append(Action(ActionType.DELETE_LOCAL, rel, lf, None, prior))
                continue

            # 9가지 시나리오
            if not prior and lf and not rf:
                actions.append(Action(ActionType.UPLOAD_NEW, rel, lf, None, None))

            elif not prior and not lf and rf:
                actions.append(Action(ActionType.DOWNLOAD_NEW, rel, None, rf, None))

            elif not prior and lf and rf:
                if self._same_content(lf, rf):
                    actions.append(Action(ActionType.SKIP_SAME, rel, lf, rf, None))
                else:
                    actions.append(self._conflict_action(rel, lf, rf, None))

            elif prior and lf and not rf:
                if self._local_changed(lf, prior):
                    actions.append(Action(ActionType.UPLOAD_NEW, rel, lf, None, prior))
                else:
                    actions.append(Action(ActionType.DELETE_LOCAL, rel, lf, None, prior))

            elif prior and not lf and rf:
                if self._remote_changed(rf, prior):
                    actions.append(Action(ActionType.DOWNLOAD_NEW, rel, None, rf, prior))
                else:
                    actions.append(Action(ActionType.DELETE_REMOTE, rel, None, rf, prior))

            elif prior and lf and rf:
                local_chg = self._local_changed(lf, prior)
                remote_chg = self._remote_changed(rf, prior)
                if local_chg and remote_chg:
                    if self._same_content(lf, rf):
                        actions.append(Action(ActionType.SKIP_SAME, rel, lf, rf, prior))
                    else:
                        actions.append(self._conflict_action(rel, lf, rf, prior))
                elif local_chg:
                    actions.append(Action(ActionType.UPLOAD_UPDATE, rel, lf, rf, prior))
                elif remote_chg:
                    actions.append(Action(ActionType.DOWNLOAD_UPDATE, rel, lf, rf, prior))
                else:
                    actions.append(Action(ActionType.SKIP_SAME, rel, lf, rf, prior))

            elif prior and not lf and not rf:
                actions.append(Action(ActionType.REMOVE_STATE, rel, None, None, prior))

        return self._detect_renames(actions)

    # ──────────────────────────────────────────────
    # rename/이동 감지 (post-pass)
    # ──────────────────────────────────────────────
    def _detect_renames(self, actions: list[Action]) -> list[Action]:
        """삭제+신규 쌍 중 내용이 동일(size+md5)한 것을 '이동'으로 변환.

        폴더/파일 rename 은 3-way diff 에서 '옛 경로 삭제 + 새 경로 신규'로
        보이기 때문에, 그대로 두면 전체 재전송(느림) + Drive 에 빈 폴더 잔재가
        남는다. 여기서 짝을 맞춰:
          - DELETE_REMOTE + UPLOAD_NEW  → MOVE_REMOTE (Drive 서버측 이동, 전송 0바이트)
          - DELETE_LOCAL + DOWNLOAD_NEW → MOVE_LOCAL  (로컬 파일 이동, 전송 0바이트)

        매칭 기준은 내용 동일성(size + md5)뿐이므로 어느 쪽이 이동해도 결과
        바이트는 같다. 0바이트 파일은 md5 가 모두 같아 짝이 모호하므로 제외
        (기존 삭제+재전송 경로로 처리 — 비용 미미).
        로컬 md5 는 삭제 후보와 size 가 일치할 때만 lazy 계산 → 평상시 비용 0.
        """
        moves: list[Action] = []
        dropped: set[int] = set()

        # ── 로컬 rename → Drive 이동
        rdel_by_size: dict[int, list[Action]] = {}
        for a in actions:
            if (a.type is ActionType.DELETE_REMOTE and a.remote
                    and a.remote.md5 and a.remote.size > 0):
                rdel_by_size.setdefault(a.remote.size, []).append(a)
        if rdel_by_size:
            for up in actions:
                if (up.type is not ActionType.UPLOAD_NEW or up.remote is not None
                        or not up.local or up.local.size <= 0):
                    continue
                cands = rdel_by_size.get(up.local.size)
                if not cands:
                    continue
                try:
                    lmd5 = up.local.md5()
                except OSError:
                    continue  # 스캔 후 사라짐/잠김 — 기존 업로드 경로로
                if not lmd5:
                    continue
                for i, d in enumerate(cands):
                    if d.remote.md5 == lmd5:
                        cands.pop(i)
                        dropped.add(id(up))
                        dropped.add(id(d))
                        moves.append(Action(
                            ActionType.MOVE_REMOTE, up.rel_path,
                            local=up.local, remote=d.remote, prior=d.prior,
                            move_from=d.rel_path,
                        ))
                        break

        # ── Drive rename → 로컬 이동
        ldel_by_size: dict[int, list[Action]] = {}
        for a in actions:
            if (a.type is ActionType.DELETE_LOCAL and a.local
                    and a.local.size > 0 and id(a) not in dropped):
                ldel_by_size.setdefault(a.local.size, []).append(a)
        if ldel_by_size:
            for dn in actions:
                if (dn.type is not ActionType.DOWNLOAD_NEW or dn.local is not None
                        or not dn.remote or not dn.remote.md5 or dn.remote.size <= 0):
                    continue
                cands = ldel_by_size.get(dn.remote.size)
                if not cands:
                    continue
                for i, d in enumerate(cands):
                    try:
                        same = (d.local.md5() == dn.remote.md5)
                    except OSError:
                        continue
                    if same:
                        cands.pop(i)
                        dropped.add(id(dn))
                        dropped.add(id(d))
                        moves.append(Action(
                            ActionType.MOVE_LOCAL, dn.rel_path,
                            local=d.local, remote=dn.remote, prior=d.prior,
                            move_from=d.rel_path,
                        ))
                        break

        if not moves:
            return actions
        log.info(f"➜ 이동/이름변경 감지: {len(moves)}건 — 재전송 대신 이동으로 처리")
        return [a for a in actions if id(a) not in dropped] + moves

    def _local_changed(self, lf: LocalFile, prior: FileState) -> bool:
        # ⚠️ 0바이트 파일 주의: `prior.local_size or -1`은 0을 -1로 바꿔버려서
        #    실제 크기 0과 항상 "다름"으로 판정 → 무한 재업로드 버그.
        #    반드시 `is None` 으로 비교해야 0바이트 파일이 안정화됨.
        expected_size = prior.local_size if prior.local_size is not None else -1
        if lf.size != expected_size:
            return True
        # mtime은 FS 정밀도(FAT 2s, SMB 1s, APFS ns) 차이로 ISO 문자열이
        # 같은 파일에 대해서도 미세하게 어긋날 수 있어 2초 허용오차로 비교.
        return not mtime_close(lf.mtime_iso, prior.local_mtime or "", tol_sec=2.0)

    def _remote_changed(self, rf: DriveFile, prior: FileState) -> bool:
        if rf.md5 and prior.remote_md5:
            return rf.md5 != prior.remote_md5
        # 동일 이유로 remote_size도 None 체크
        expected_size = prior.remote_size if prior.remote_size is not None else -1
        if rf.size != expected_size:
            return True
        # Drive 응답이 ms 단위지만 권한 변경 등 메타 수정에도 modifiedTime이
        # 흔들리므로 2초 허용오차.
        return not mtime_close(rf.modified_time, prior.remote_mtime or "", tol_sec=2.0)

    def _same_content(self, lf: LocalFile, rf: DriveFile) -> bool:
        if not (rf.md5 and lf.size == rf.size):
            return False
        # C-2: 큰 파일은 MD5(전체 디스크 읽기) 대신 mtime 근접도로 매칭.
        # 첫 동기화/state 손실 시 12-30분 걸리던 검토 단계를 수 초로 단축.
        # size 동일 + mtime ±2초 이내 → 동일 파일로 간주 (false-skip 위험 매우 낮음).
        threshold_mb = getattr(self.cfg.performance, "large_file_md5_skip_mb", 0)
        if threshold_mb > 0 and lf.size >= threshold_mb * 1024 * 1024:
            if mtime_close(lf.mtime_iso, rf.modified_time, tol_sec=2.0):
                log.debug(
                    f"size+mtime 매칭 (MD5 생략): {lf.rel_path} "
                    f"({lf.size/1024/1024:.0f} MB)"
                )
                return True
            # mtime 차이 크면 다른 파일로 간주 — 충돌 흐름으로
            return False
        return lf.md5() == rf.md5

    def _files_match(
        self,
        lf: LocalFile,
        rf: DriveFile,
        prior: Optional[FileState],
    ) -> bool:
        """양쪽이 동일 내용인지. 미러 모드에서 재전송 회피용.

        1) prior 기반 빠른 판정: 양쪽 모두 prior 대비 변경 없으면 동일.
           (해시 계산 없이 size/mtime 메타만 비교 — 대량 파일도 빠름)
        2) prior 없거나 한쪽이 변경됐으면 MD5 기반 정확 비교 (`_same_content`).
        """
        if prior and not self._local_changed(lf, prior) and not self._remote_changed(rf, prior):
            return True
        return self._same_content(lf, rf)

    def _conflict_action(
        self,
        rel: str,
        lf: LocalFile,
        rf: DriveFile,
        prior: Optional[FileState],
    ) -> Action:
        decision = resolve_conflict(
            self.cfg.conflict_policy,
            rel,
            lf.mtime_iso,
            rf.modified_time,
        )
        if decision.action == ConflictResolution.UPLOAD:
            return Action(ActionType.CONFLICT_UPLOAD, rel, lf, rf, prior)
        if decision.action == ConflictResolution.DOWNLOAD:
            return Action(ActionType.CONFLICT_DOWNLOAD, rel, lf, rf, prior)
        return Action(
            ActionType.CONFLICT_KEEP_BOTH, rel, lf, rf, prior,
            rename_to=decision.rename_to,
        )

    # ──────────────────────────────────────────────
    # 실행 (v2: 병렬)
    # ──────────────────────────────────────────────
    def _execute(
        self,
        pair: SyncPair,
        actions: list[Action],
        remote_root_id: str,
        state: SyncState,
        summary: SyncSummary,
    ) -> None:
        import signal

        # Ctrl+C 처리 — 함수 종료 시 원래 핸들러를 반드시 복원해야
        # CLI/GUI의 평소 SIGINT 동작이 영구히 바뀌지 않는다.
        pool_ref = {"pool": None}
        def _on_sigint(signum, frame):
            log.warning("\n중단 요청 감지 — 진행 중인 전송 완료 후 안전하게 종료합니다...")
            if pool_ref["pool"]:
                pool_ref["pool"].request_stop()
        _prev_sigint = None
        _sigint_installed = False
        try:
            _prev_sigint = signal.signal(signal.SIGINT, _on_sigint)
            _sigint_installed = True
        except (ValueError, AttributeError):
            pass

        try:
            self._execute_inner(pair, actions, remote_root_id, state, summary, pool_ref)
        finally:
            if _sigint_installed:
                try:
                    signal.signal(signal.SIGINT, _prev_sigint or signal.SIG_DFL)
                except (ValueError, AttributeError):
                    pass

    def _execute_inner(
        self,
        pair: SyncPair,
        actions: list[Action],
        remote_root_id: str,
        state: SyncState,
        summary: SyncSummary,
        pool_ref: dict,
    ) -> None:
        # 분류: 병렬 대상 vs 순차 대상
        parallel_actions = [a for a in actions if a.type in _PARALLEL_TYPES]
        sequential_actions = [a for a in actions if a.type not in _PARALLEL_TYPES]

        # ── 1) 순차 작업 먼저 (삭제/스킵/REMOVE_STATE/KEEP_BOTH)
        for action in sequential_actions:
            try:
                self._execute_sequential_one(pair, action, remote_root_id, state, summary)
            except Exception as e:
                log.error(f"순차 작업 실패 [{action.type.value}] {action.rel_path}: {e}")
                summary.errors += 1

        if not parallel_actions:
            return

        # ── 2) 병렬 업/다운 전에 필요한 상위 폴더 사전 생성
        self._precreate_parent_folders(pair, parallel_actions)

        # 강제 중단된 경우 → 병렬 전송 건너뜀
        # (file/pair-level stop은 현재 폴더는 끝까지)
        if self.is_force_stop_requested():
            log.warning("강제 중단 — 파일 전송 단계 건너뜀.")
            return

        # ── 3) 업로드/다운로드 태스크 빌드
        upload_tasks: list[UploadTask] = []
        download_tasks: list[DownloadTask] = []
        locked_skipped: list[Action] = []

        for action in parallel_actions:
            if action.type in _UPLOAD_TYPES:
                lf = action.local
                if lf and is_file_locked(lf.abs_path):
                    log.warning(f"파일 잠김 (건너뜀): {action.rel_path}")
                    locked_skipped.append(action)
                    summary.skipped += 1
                    continue

                # 상위 폴더 ID 조회 (사전 생성돼 있어 캐시 히트)
                parent_rel = to_posix(Path(action.rel_path).parent)
                if parent_rel in (".", ""):
                    parent_id = remote_root_id
                else:
                    full = f"{pair.remote_path}/{parent_rel}" if pair.remote_path else parent_rel
                    parent_id = self.drive.resolve_folder_path(full, create_missing=True)

                upload_tasks.append(UploadTask(
                    rel_path=action.rel_path,
                    local=lf,
                    parent_id=parent_id,
                    existing_id=action.remote.id if action.remote else None,
                    is_conflict=(action.type == ActionType.CONFLICT_UPLOAD),
                ))

            elif action.type in _DOWNLOAD_TYPES:
                rf = action.remote
                if not rf:
                    continue
                dest = pair.local_path.joinpath(*action.rel_path.split("/"))
                download_tasks.append(DownloadTask(
                    rel_path=action.rel_path,
                    remote=rf,
                    dest_path=dest,
                    is_conflict=(action.type == ActionType.CONFLICT_DOWNLOAD),
                ))

        # ── 4) TransferPool 실행
        pool = TransferPool(
            cfg=self.cfg,
            base_drive=self.drive,
            bandwidth_limiter=self.bandwidth,
            progress_factory=self.progress_factory,
        )
        # ProgressTracker 와 GUI 콜백 모두 호출하는 래퍼
        # — TransferPool은 progress_callback(completed_in_pair, total_in_pair, rel_path) 호출
        _last_completed = [0]
        gui_cb = self.progress_callback
        tracker = self.progress_tracker

        def _wrapped_progress(completed, total, rel_path):
            # GUI 콜백 (현재 폴더 % 표시용)
            if gui_cb:
                try:
                    gui_cb(completed, total, rel_path)
                except Exception:
                    pass
            # ProgressTracker (전체 진행 — 파일 카운트만)
            if tracker:
                try:
                    delta = completed - _last_completed[0]
                    if delta > 0:
                        for _ in range(delta):
                            tracker.on_file_done(bytes_delta=0)
                        _last_completed[0] = completed
                except Exception:
                    pass

        def _wrapped_bytes(bytes_delta):
            # ProgressTracker 에 바이트만 누적 (file 카운트는 _wrapped_progress 가 담당)
            if tracker:
                try:
                    tracker.bytes_done += bytes_delta
                except Exception:
                    pass

        pool.progress_callback = _wrapped_progress
        # 청크 단위로 bytes_done 갱신 — 트리클 워치독이 큰 파일 중간 정지를 빠르게 감지.
        # chunk_byte_callback 설정 시 transfer_pool._run_batch 는 per-completion
        # byte_callback 을 자동으로 스킵해 이중 카운트 방지.
        pool.chunk_byte_callback = _wrapped_bytes
        pool_ref["pool"] = pool
        self._current_pool = pool   # GUI에서 직접 중단 접근용

        log.info(
            f"병렬 전송 시작: 업로드 {len(upload_tasks)}개 / "
            f"다운로드 {len(download_tasks)}개 "
            f"(동시 {self.cfg.performance.parallel_transfers}개)"
        )

        upload_results = pool.execute_uploads(pair, upload_tasks)
        download_results = pool.execute_downloads(pair, download_tasks)

        # ── 5) 결과를 state/summary에 반영 (메인 스레드 독점)
        self._apply_results(upload_results + download_results, state, summary)

    # ──────────────────────────────────────────────
    # 사전 폴더 생성 (병렬 경쟁 방지)
    # ──────────────────────────────────────────────
    def _precreate_parent_folders(
        self,
        pair: SyncPair,
        parallel_actions: list[Action],
    ) -> None:
        parents: set[str] = set()
        for action in parallel_actions:
            if action.type not in _UPLOAD_TYPES:
                continue
            parent_rel = to_posix(Path(action.rel_path).parent)
            if parent_rel in (".", ""):
                continue
            parents.add(parent_rel)

        if not parents:
            return

        # 얕은 폴더부터 순차 생성 → 각 상위가 먼저 캐시에 박힘
        sorted_parents = sorted(parents, key=lambda p: (p.count("/"), p))
        total = len(sorted_parents)
        milestone_interval = max(1, total // 5) if total > 5 else 1
        log_every = (total <= 5)

        log.info(f"폴더 구조 생성 중: {total}개")

        for i, parent_rel in enumerate(sorted_parents, 1):
            # 강제 중단 시에만 폴더 생성 중간에 빠져나옴
            if self.is_force_stop_requested():
                log.warning(
                    f"강제 중단 — 폴더 생성 {i-1}/{total}까지, "
                    f"{total - i + 1}개 남음"
                )
                return

            full = f"{pair.remote_path}/{parent_rel}" if pair.remote_path else parent_rel
            try:
                self.drive.resolve_folder_path(full, create_missing=True)
            except Exception as e:
                log.warning(f"폴더 사전 생성 실패 {full}: {e}")
                continue

            # 마일스톤 로그 (20% 단위)
            if log_every:
                log.info(f"[폴더 {i}/{total}] {parent_rel}")
            elif i == 1 or i == total or i % milestone_interval == 0:
                pct = int(i / total * 100)
                log.info(f"[폴더 진행 {pct}%] {i}/{total} 생성 완료")

            # 프로그래스 콜백 (프로그래스 바 업데이트)
            if self.progress_callback:
                try:
                    self.progress_callback(i, total, f"폴더: {parent_rel}")
                except Exception:
                    pass

    # ──────────────────────────────────────────────
    # 결과 병합
    # ──────────────────────────────────────────────
    def _apply_results(
        self,
        results: list[TransferResult],
        state: SyncState,
        summary: SyncSummary,
    ) -> None:
        with self._state_lock:
            for r in results:
                if not r.success:
                    if getattr(r, "vanished", False):
                        # 스캔 후 사라진 파일은 오류가 아닌 소프트 스킵
                        summary.vanished += 1
                        # basename 빈도 누적 (요약에 Top 3 표시)
                        bn = Path(r.rel_path).name
                        summary.vanished_samples[bn] = (
                            summary.vanished_samples.get(bn, 0) + 1
                        )
                    else:
                        summary.errors += 1
                    continue
                if r.file_state:
                    state.files[r.rel_path] = r.file_state
                if r.direction == "upload":
                    summary.uploaded += 1
                    summary.uploaded_bytes += r.bytes_done
                else:
                    summary.downloaded += 1
                    summary.downloaded_bytes += r.bytes_done
                if r.is_conflict:
                    summary.conflicts += 1

    # ──────────────────────────────────────────────
    # 순차 실행 (삭제/keep_both/remove_state/skip)
    # ──────────────────────────────────────────────
    def _execute_sequential_one(
        self,
        pair: SyncPair,
        action: Action,
        remote_root_id: str,
        state: SyncState,
        summary: SyncSummary,
    ) -> None:
        t = action.type
        rel = action.rel_path

        if t in (ActionType.SKIP_SAME, ActionType.SKIP_LOCKED, ActionType.SKIP_WORKSPACE):
            summary.skipped += 1
            if t == ActionType.SKIP_SAME and action.local and action.remote:
                state.files[rel] = self._make_state(action.local, action.remote)
            return

        if t == ActionType.REMOVE_STATE:
            # ⚠ 양쪽 모두에서 사라진 항목 — 정상 케이스(사용자가 양쪽에서 의도적으로 삭제)일 수도,
            # 비정상 케이스(Drive list_tree 일시 누락 + 로컬 정리가 겹쳐 ghost 처리됨)일 수도 있음.
            # 후자 진단을 위해 카운트 + 샘플 + 로그 명시. 대량 발생은 _summarize_results 에서 경고.
            state.files.pop(rel, None)
            summary.removed_state += 1
            # 샘플은 처음 10개까지만 (대량이어도 메모리 제한)
            if len(summary.removed_state_samples) < 10:
                summary.removed_state_samples.append(rel)
            log.info(f"⊖ state 정리(양쪽 사라짐): {rel}")
            return

        if t == ActionType.DELETE_LOCAL:
            self._do_delete_local(pair, action, state, summary)
            return

        if t == ActionType.DELETE_REMOTE:
            self._do_delete_remote(action, state, summary)
            return

        if t == ActionType.MOVE_REMOTE:
            self._do_move_remote(pair, action, remote_root_id, state, summary)
            return

        if t == ActionType.MOVE_LOCAL:
            self._do_move_local(pair, action, state, summary)
            return

        if t == ActionType.CONFLICT_KEEP_BOTH:
            self._do_keep_both(pair, action, remote_root_id, state, summary)
            return

    def _make_state(self, lf: LocalFile, rf: DriveFile) -> FileState:
        return FileState(
            local_size=lf.size,
            local_mtime=lf.mtime_iso,
            local_md5=lf._md5,
            remote_id=rf.id,
            remote_size=rf.size,
            remote_mtime=rf.modified_time,
            remote_md5=rf.md5,
            mime_type=rf.mime_type,
        )

    def _do_delete_local(self, pair, action, state, summary):
        policy = self.cfg.delete_policy
        if policy == "skip":
            return
        if not action.local:
            state.files.pop(action.rel_path, None)
            return

        target = action.local.abs_path
        if policy == "trash":
            dest = self._resolve_trash_dest(pair, action.rel_path)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(dest))
            except Exception as e:
                log.error(f"휴지통 이동 실패: {e}")
                return
            log.info(f"🗑 로컬 휴지통: {action.rel_path}")
        else:
            try:
                target.unlink()
            except Exception as e:
                log.error(f"삭제 실패: {e}")
                return
            log.info(f"🗑 로컬 영구삭제: {action.rel_path}")

        summary.deleted_local += 1
        state.files.pop(action.rel_path, None)
        # 파일이 사라져 비게 된 상위 폴더(껍데기) 정리 — 크로스플랫폼
        self._prune_empty_dirs(pair, target.parent, summary)

    def _prune_empty_dirs(self, pair: SyncPair, start_dir: Path, summary=None) -> None:
        """파일 삭제로 비게 된 상위 폴더를 페어 루트 직전까지 제거.

        gdrive-sync 는 파일만 추적·삭제하므로, 폴더 안 파일이 모두 삭제되면
        빈 폴더 껍데기가 로컬에 남는다(Windows/macOS/Linux 공통 — 순수 파이썬
        삭제 경로라 OS 분기 없음). 이 헬퍼가 그 껍데기를 정리한다.

        안전망:
        - 페어 루트(`pair.local_path`) 자체와 그 바깥은 절대 건드리지 않음.
        - 실제 파일/하위폴더가 하나라도 있으면 즉시 중단(위로 안 올라감).
        - OS 메타데이터(`_PRUNABLE_JUNK`)만 남은 폴더는 비어있는 것으로 간주해
          그 junk 를 지운 뒤 폴더 제거. junk 제거나 rmdir 실패 시 안전하게 중단.
        - 순수 pathlib 만 사용 → Windows/macOS/Linux 동일 동작.
        """
        try:
            root = pair.local_path.resolve()
        except Exception:
            return
        cur = start_dir
        while True:
            try:
                cur_res = cur.resolve()
            except Exception:
                return
            # 루트 자체이거나 루트 하위가 아니면 중단 (루트는 보존)
            if cur_res == root or root not in cur_res.parents:
                return
            if not cur.is_dir():
                return
            try:
                entries = list(cur.iterdir())
            except Exception:
                return
            leftover = [e for e in entries if e.name not in _PRUNABLE_JUNK]
            if leftover:
                return  # 실제 내용 남음 → 더 위로 올라가지 않음
            # junk 만 남음(또는 완전히 빔) → junk 제거 후 폴더 제거 시도
            for e in entries:
                try:
                    e.unlink()
                except Exception:
                    return  # junk 못 지우면 폴더도 못 비움 → 중단
            try:
                cur.rmdir()
            except OSError:
                return  # 경쟁 조건/비어있지 않음 → 중단
            try:
                rel = cur.relative_to(root)
            except Exception:
                rel = cur.name
            log.info(f"🗑 빈 폴더 제거: {rel}")
            if summary is not None:
                summary.pruned_dirs += 1
            cur = cur.parent

    def _resolve_trash_dest(self, pair: SyncPair, rel_path: str) -> Path:
        """휴지통 내 목적지 경로 결정.

        central=True (기본): ~/.gdrive_sync/trash/<폴더명>/<원래 상대경로>
        central=False: <pair.local_path>/.gdrive_sync_trash/<rel_path "/"→"_">

        같은 이름 파일이 이미 있으면 _1, _2 접미사로 충돌 회피.
        """
        trash_cfg = self.cfg.trash
        if trash_cfg.central:
            if trash_cfg.central_path:
                base = Path(trash_cfg.central_path).expanduser()
            else:
                base = Path.home() / ".gdrive_sync" / "trash"
            # 폴더명 분리: "Downloads/sub/file.txt" → trash/Downloads/sub/file.txt
            pair_name = pair.local_path.name or "_root"
            dest = base / pair_name / rel_path
        else:
            dest = pair.local_path / ".gdrive_sync_trash" / rel_path.replace("/", "_")

        # 충돌 회피
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            n = 1
            while True:
                candidate = dest.parent / f"{stem}_{n}{suffix}"
                if not candidate.exists():
                    dest = candidate
                    break
                n += 1
                if n > 9999:
                    break
        return dest

    def _do_delete_remote(self, action, state, summary):
        policy = self.cfg.delete_policy
        if policy == "skip":
            return
        if not action.remote:
            state.files.pop(action.rel_path, None)
            return

        permanent = (policy == "permanent")
        try:
            self.drive.delete_file(action.remote.id, permanent=permanent)
        except Exception as e:
            log.error(f"리모트 삭제 실패: {e}")
            return

        kind = "영구삭제" if permanent else "휴지통"
        log.info(f"🗑 Drive {kind}: {action.rel_path}")
        summary.deleted_remote += 1
        state.files.pop(action.rel_path, None)
        # 파일이 빠져나가 비었을 수 있는 상위 폴더 — 실행 후 일괄 정리 대상 등록
        parent_rel = to_posix(Path(action.rel_path).parent)
        if parent_rel not in (".", ""):
            self._remote_prune_rels.add(parent_rel)

    # ──────────────────────────────────────────────
    # 이동 실행 (rename 감지 결과)
    # ──────────────────────────────────────────────
    def _do_move_remote(self, pair, action, remote_root_id, state, summary):
        """로컬 rename/이동을 Drive 서버측 이동으로 반영 (전송 0바이트).

        modifiedTime 을 원본 그대로 유지해 다른 PC 가 '내용 변경'으로
        오판하지 않도록 함 (rename 의 modifiedTime 부작용 방지).
        """
        rf = action.remote
        lf = action.local
        if not rf or not lf:
            return

        new_name = action.rel_path.split("/")[-1]
        parent_rel = to_posix(Path(action.rel_path).parent)
        try:
            if parent_rel in (".", ""):
                parent_id = remote_root_id
            else:
                full = f"{pair.remote_path}/{parent_rel}" if pair.remote_path else parent_rel
                parent_id = self.drive.resolve_folder_path(full, create_missing=True)
            old_parent_id = rf.parents[0] if rf.parents else None
            result = self.drive.move_file(
                rf.id,
                new_parent_id=parent_id,
                old_parent_id=old_parent_id,
                new_name=new_name if new_name != rf.name else None,
                keep_modified_time=rf.modified_time,
            )
        except Exception as e:
            log.error(f"Drive 이동 실패 {action.move_from} → {action.rel_path}: {e}")
            summary.errors += 1
            return  # 다음 동기화에서 삭제+업로드로 자연 복구됨

        log.info(f"➜ Drive 이동: {action.move_from} → {action.rel_path}")
        if action.move_from:
            state.files.pop(action.move_from, None)
            old_parent = to_posix(Path(action.move_from).parent)
            if old_parent not in (".", ""):
                self._remote_prune_rels.add(old_parent)
        # 이동 응답 메타가 비면 원본 메타로 보강 (내용은 안 바뀌었으므로 동일)
        state.files[action.rel_path] = FileState(
            local_size=lf.size,
            local_mtime=lf.mtime_iso,
            local_md5=lf._md5,
            remote_id=result.id or rf.id,
            remote_size=result.size or rf.size,
            remote_mtime=result.modified_time or rf.modified_time,
            remote_md5=result.md5 or rf.md5,
            mime_type=result.mime_type or rf.mime_type,
        )
        summary.moved_remote += 1

    def _do_move_local(self, pair, action, state, summary):
        """Drive 쪽 rename/이동을 로컬 파일 이동으로 반영 (전송 0바이트)."""
        rf = action.remote
        lf = action.local
        if not rf or not lf:
            return

        src = lf.abs_path
        dest = pair.local_path.joinpath(*action.rel_path.split("/"))
        if dest.exists():
            # 제외 패턴 등으로 스캔에 안 잡힌 실제 파일이 있을 수 있음 —
            # 덮어쓰지 않고 스킵. 다음 동기화에서 삭제+다운로드로 재처리.
            log.warning(
                f"이동 목적지에 파일이 이미 있어 생략: "
                f"{action.move_from} → {action.rel_path}"
            )
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except Exception as e:
            log.error(f"로컬 이동 실패 {action.move_from} → {action.rel_path}: {e}")
            summary.errors += 1
            return

        log.info(f"➜ 로컬 이동: {action.move_from} → {action.rel_path}")
        if action.move_from:
            state.files.pop(action.move_from, None)
        st = dest.stat()
        state.files[action.rel_path] = FileState(
            local_size=st.st_size,
            local_mtime=mtime_to_iso(st.st_mtime),
            local_md5=lf._md5,
            remote_id=rf.id,
            remote_size=rf.size,
            remote_mtime=rf.modified_time,
            remote_md5=rf.md5,
            mime_type=rf.mime_type,
        )
        summary.moved_local += 1
        # 파일이 빠져나가 비었을 수 있는 옛 상위 폴더 정리 (로컬)
        self._prune_empty_dirs(pair, src.parent, summary)

    # ──────────────────────────────────────────────
    # Drive 빈 폴더 정리 (파일 삭제/이동 후)
    # ──────────────────────────────────────────────
    def _prune_empty_remote_dirs(self, pair: SyncPair, summary: SyncSummary) -> None:
        """이번 실행에서 파일이 삭제/이동돼 비게 된 Drive 폴더를 bottom-up 정리.

        로컬의 `_prune_empty_dirs` 와 대칭. 폴더 rename 후 옛 이름 폴더가
        Drive 에 껍데기로 남는 문제의 해결책. 안전망:
        - 이번 실행에서 삭제/이동이 일어난 경로의 상위만 검사 (전수 스캔 아님).
        - 자식이 하나라도 남아 있으면 보존하고 위로 안 올라감.
        - 페어 루트는 절대 건드리지 않음.
        - delete_policy 를 따름 (skip 이면 정리 안 함, 기본 trash 는 복구 가능).
        """
        if not self._remote_prune_rels:
            return
        policy = self.cfg.delete_policy
        if policy == "skip":
            return
        permanent = (policy == "permanent")

        kept: set[str] = set()
        removed: set[str] = set()
        # 깊은 경로부터 — 자식 폴더가 먼저 비워져야 부모도 빈 것으로 판정됨
        for rel in sorted(self._remote_prune_rels, key=lambda r: -r.count("/")):
            cur = rel
            while cur:
                if self.is_force_stop_requested():
                    return
                if cur in kept:
                    break
                if cur in removed:
                    cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
                    continue
                full = f"{pair.remote_path}/{cur}" if pair.remote_path else cur
                try:
                    fid = self.drive.resolve_folder_path(full, create_missing=False)
                except FileNotFoundError:
                    # 이미 사라진 폴더 — 부모는 계속 검사
                    removed.add(cur)
                    cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
                    continue
                except Exception as e:
                    log.warning(f"Drive 폴더 조회 실패(정리 스킵) {full}: {e}")
                    break
                try:
                    if self.drive.folder_has_children(fid):
                        kept.add(cur)
                        break
                    self.drive.delete_file(fid, permanent=permanent)
                except Exception as e:
                    log.warning(f"Drive 빈 폴더 정리 실패 {full}: {e}")
                    break
                self.drive.invalidate_cached_path(full)
                removed.add(cur)
                summary.pruned_remote_dirs += 1
                log.info(f"🗑 Drive 빈 폴더 제거: {cur}")
                cur = cur.rsplit("/", 1)[0] if "/" in cur else ""
        self._remote_prune_rels = set()

    def _do_keep_both(self, pair, action, remote_root_id, state, summary):
        """충돌 파일을 _conflict_ 접미사로 rename한 뒤 양쪽 보존.

        keep_both는 순차 처리 (다운로드 + 복사 + 업로드 조합이라 병렬 불필요).
        """
        rename_to = action.rename_to or action.rel_path
        lf = action.local
        if not lf:
            return

        # 로컬 복사본 생성
        new_local = pair.local_path.joinpath(*rename_to.split("/"))
        new_local.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lf.abs_path, new_local)

        # 복사본 업로드 (단일 스레드 DriveClient 사용)
        parent_rel = to_posix(Path(rename_to).parent)
        if parent_rel in (".", ""):
            parent_id = remote_root_id
        else:
            full = f"{pair.remote_path}/{parent_rel}" if pair.remote_path else parent_rel
            parent_id = self.drive.resolve_folder_path(full, create_missing=True)

        try:
            result = self.drive.upload_file(
                new_local,
                parent_id=parent_id,
                name=Path(rename_to).name,
                bandwidth_limiter=self.bandwidth,
                strategy="auto",
            )
            summary.uploaded += 1
            summary.uploaded_bytes += new_local.stat().st_size
            state.files[rename_to] = FileState(
                local_size=new_local.stat().st_size,
                local_mtime=mtime_to_iso(new_local.stat().st_mtime),
                remote_id=result.id,
                remote_size=result.size,
                remote_mtime=result.modified_time,
                remote_md5=result.md5,
                mime_type=result.mime_type,
            )
        except Exception as e:
            log.error(f"keep_both 업로드 실패: {e}")
            summary.errors += 1
            return

        # 원본 경로는 리모트 버전으로 덮어쓰기 (다운로드)
        if action.remote:
            try:
                dest = pair.local_path.joinpath(*action.rel_path.split("/"))
                self.drive.download_file(
                    action.remote.id,
                    dest,
                    bandwidth_limiter=self.bandwidth,
                )
                st = dest.stat()
                summary.downloaded += 1
                summary.downloaded_bytes += st.st_size
                state.files[action.rel_path] = FileState(
                    local_size=st.st_size,
                    local_mtime=mtime_to_iso(st.st_mtime),
                    remote_id=action.remote.id,
                    remote_size=action.remote.size,
                    remote_mtime=action.remote.modified_time,
                    remote_md5=action.remote.md5,
                    mime_type=action.remote.mime_type,
                )
            except Exception as e:
                log.error(f"keep_both 다운로드 실패: {e}")
                summary.errors += 1
                return

        summary.conflicts += 1
