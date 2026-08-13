"""병렬 파일 전송 매니저.

설계 원칙:
1. ThreadPoolExecutor 기반 (네트워크 I/O는 GIL 풀리므로 스레드가 효율적)
2. 스레드별 독립 DriveClient (httplib2.Http는 스레드 안전하지 않음)
3. 그러나 credentials와 path_cache는 전역 공유 (토큰 재활용 + 폴더 중복 생성 방지)
4. 파일 크기별 전략:
   - < 5MB: simple upload (1 RTT, 최소 오버헤드)
   - 5~100MB: resumable, 8MB 청크
   - >= 100MB: resumable, 32MB 청크
5. BandwidthLimiter 공유 → 전체 대역폭 합이 제한값 이하로 수렴
6. 개별 파일 실패 격리 (다른 파일 전송 계속)
7. state 업데이트는 Lock으로 보호
8. 작은 파일 우선 정렬 (오버헤드 비율 큰 파일부터 빠르게 소진 → 병렬 효과 극대화)

※ 제거된 설계 (Cowork 원안과 차이):
- Semaphore(3) "초당 3회": 개념 오류였음. Semaphore는 동시성 제한이지 rate limit이 아님.
  429 에러는 DriveClient._retry가 이미 exponential backoff로 처리하므로 불필요.
- connection_pool_size: httplib2에 풀 개념이 없어 의미 없음. 스레드별 Http 인스턴스로 대체.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from gdrive_sync.bandwidth import BandwidthLimiter
from gdrive_sync.config import Config, SyncPair
from gdrive_sync.drive_api import DriveClient, DriveFile
from gdrive_sync.local_scanner import LocalFile
from gdrive_sync.state import FileState
from gdrive_sync.utils import mtime_to_iso, parse_rfc3339, to_posix


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 전송 결과
# ──────────────────────────────────────────────────────────

@dataclass
class TransferResult:
    """워커 스레드가 반환하는 결과.

    메인 스레드가 state 업데이트 및 요약 집계에 사용.
    """
    rel_path: str
    direction: str          # "upload" | "download"
    success: bool
    error: Optional[str] = None
    file_state: Optional[FileState] = None    # 성공 시 state에 반영할 데이터
    bytes_done: int = 0
    is_conflict: bool = False                 # 충돌 처리 결과 카운트용
    # 스캔 후 업로드 직전에 파일이 사라진 경우(AV/DLP 임시파일 등)
    # 진짜 오류가 아니므로 별도 카운트
    vanished: bool = False


# ──────────────────────────────────────────────────────────
# 병렬 전송 매니저
# ──────────────────────────────────────────────────────────

class TransferPool:
    """여러 파일을 스레드 풀로 동시 전송."""

    def __init__(
        self,
        cfg: Config,
        base_drive: DriveClient,
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
        progress_factory: Optional[Callable] = None,
    ):
        self.cfg = cfg
        self.base_drive = base_drive
        self.bandwidth = bandwidth_limiter
        self.progress_factory = progress_factory

        self.max_workers = max(1, min(10, cfg.performance.parallel_transfers))

        # 모든 워커가 공유할 자원
        self._credentials = base_drive.credentials
        self._shared_path_cache, self._path_cache_lock = base_drive.shared_path_cache

        # 스레드별 독립 DriveClient
        self._tls = threading.local()

        # 중단 플래그
        self._stop_requested = threading.Event()

        # 진행 상황 콜백: progress_callback(completed, total, rel_path)
        self.progress_callback: Optional[Callable] = None

        # 바이트 누적 콜백: byte_callback(bytes_delta_for_this_task)
        # 호출 시점은 각 task 완료 직후 (성공한 transfer의 bytes_done만 합산)
        self.byte_callback: Optional[Callable] = None

        # 청크 단위 바이트 콜백: chunk_byte_callback(delta_bytes_this_chunk)
        # 청크 progress_cb 마다 (current - last_current) 만큼 호출.
        # 워치독이 큰 파일 중간 정지를 빠르게 감지하도록 하기 위함.
        # 이 콜백이 설정되면 _run_batch 는 per-completion byte_callback 을 건너뛰어
        # 이중 카운트를 방지함.
        self.chunk_byte_callback: Optional[Callable] = None

    def request_stop(self) -> None:
        """진행 중인 배치의 남은 작업 취소 (현재 청크는 완료됨)."""
        self._stop_requested.set()

    # ──────────────────────────────────────────────
    # 배치 실행 진입점
    # ──────────────────────────────────────────────
    def execute_uploads(
        self,
        pair: SyncPair,
        tasks: list["UploadTask"],
    ) -> list[TransferResult]:
        """업로드 작업 여러 개를 병렬로 실행."""
        if not tasks:
            return []
        # 작은 파일 우선 (오버헤드 비율 높은 파일부터)
        tasks = sorted(tasks, key=lambda t: t.local.size)
        return self._run_batch(
            [(t, self._do_upload) for t in tasks],
        )

    def execute_downloads(
        self,
        pair: SyncPair,
        tasks: list["DownloadTask"],
    ) -> list[TransferResult]:
        if not tasks:
            return []
        tasks = sorted(tasks, key=lambda t: t.remote.size)
        return self._run_batch(
            [(t, self._do_download) for t in tasks],
        )

    def _run_batch(self, work_items: list) -> list[TransferResult]:
        """병렬 실행 + 마일스톤 로그 + 프로그래스 콜백.

        로그 정책:
        - 파일 5개 이하: 전부 로그 (적으니까)
        - 6개 이상: 10% 마일스톤에서만 로그 (로그 폭주 방지)
        - 오류/실패: 항상 로그 (놓치면 안 됨)
        - 프로그래스 콜백: 매 파일마다 호출 (프로그래스 바용)
        """
        results: list[TransferResult] = []
        total = len(work_items)

        # 마일스톤 간격 계산: 20% 단위 (최소 1)
        milestone_interval = max(1, total // 5) if total > 5 else 1
        log_every_file = (total <= 5)

        collected: set = set()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="gdrv-xfer",
        ) as pool:
            futures = {
                pool.submit(self._safe_run, fn, task): task
                for task, fn in work_items
            }
            completed = 0
            for fut in concurrent.futures.as_completed(futures):
                collected.add(fut)
                completed += 1
                task = futures[fut]
                try:
                    result = fut.result()
                    results.append(result)
                except Exception as e:
                    log.error(f"워커 예외: {task.rel_path}: {e}")
                    result = TransferResult(
                        rel_path=task.rel_path,
                        direction=task.direction,
                        success=False,
                        error=str(e),
                    )
                    results.append(result)

                # ── 로그: 마일스톤 또는 오류만 ──
                arrow = "↑" if task.direction == "upload" else "↓"
                if not result.success and result.error != "중단됨" and not result.vanished:
                    name = task.rel_path.rsplit("/", 1)[-1] if "/" in task.rel_path else task.rel_path
                    # 오류 원문을 함께 남겨야 사후 진단 가능 (2026-08-13: 사유 없는
                    # '실패: 파일명' 19,180건으로 원인 추적 불가했던 사례)
                    reason = " ".join((result.error or "원인 미상").split())[:160]
                    log.warning(f"[{completed}/{total}] {arrow} 실패: {name} — {reason}")
                elif result.vanished:
                    # AV/DLP/Office 임시파일이 스캔 후 사라진 케이스 — 요약에만 카운트, 화면 로그는 조용히
                    name = task.rel_path.rsplit("/", 1)[-1] if "/" in task.rel_path else task.rel_path
                    log.debug(f"[{completed}/{total}] {arrow} 사라짐(스킵): {name}")
                elif result.success and log_every_file:
                    name = task.rel_path.rsplit("/", 1)[-1] if "/" in task.rel_path else task.rel_path
                    log.info(f"[{completed}/{total}] {arrow} {name}")
                elif result.success and (completed == 1 or completed == total or completed % milestone_interval == 0):
                    pct = int(completed / total * 100)
                    log.info(f"[진행 {pct}%] {completed}/{total} 완료")

                # ── 프로그래스 콜백 ──
                if self.progress_callback:
                    try:
                        self.progress_callback(completed, total, task.rel_path)
                    except Exception:
                        pass

                # ── 바이트 누적 콜백 (ETA 용) ──
                # chunk_byte_callback 이 설정돼 있으면 청크 단위로 이미 누적됐으므로 스킵.
                if (
                    self.byte_callback
                    and not self.chunk_byte_callback
                    and result.success
                    and result.bytes_done > 0
                ):
                    try:
                        self.byte_callback(result.bytes_done)
                    except Exception:
                        pass

                # ── 중단 감지 시 남은 Future 취소 ──
                if self._stop_requested.is_set():
                    cancelled = 0
                    for remaining_fut in futures:
                        if remaining_fut.cancel():
                            cancelled += 1
                    if cancelled:
                        log.info(f"중단: 대기 중인 {cancelled}개 작업 취소됨")
                    break

        # 중단 시 break로 빠져나오면 cancel 실패한 (이미 실행 중) future들의
        # 결과가 results에 누락된다 → 바이트는 업로드됐는데 state는 미갱신 →
        # 다음 sync에서 재업로드. ThreadPoolExecutor __exit__이 완료를 기다리므로
        # 여기서 빠진 결과를 수집한다.
        for fut, task in futures.items():
            if fut in collected or fut.cancelled():
                continue
            try:
                results.append(fut.result())
            except Exception as e:
                log.error(f"워커 예외(중단 후 수집): {task.rel_path}: {e}")
                results.append(TransferResult(
                    rel_path=task.rel_path,
                    direction=task.direction,
                    success=False,
                    error=str(e),
                ))

        self._log_error_breakdown(results)
        return results

    @staticmethod
    def _log_error_breakdown(results: list) -> None:
        """실패 다발 시 오류 유형별 상위 3개 집계를 로그로 — 원인 파악용."""
        errors = [
            " ".join((r.error or "원인 미상").split())[:100]
            for r in results
            if not r.success and r.error != "중단됨" and not r.vanished
        ]
        if len(errors) < 5:      # 소수 실패는 개별 실패 로그로 충분
            return
        from collections import Counter
        top = Counter(errors).most_common(3)
        lines = [f"  {cnt}건 — {msg}" for msg, cnt in top]
        log.warning("오류 유형 집계 (상위 3):\n" + "\n".join(lines))

    def is_stop_requested(self) -> bool:
        """중단이 요청됐는지 확인 (업로드/다운로드 루프에서 호출)."""
        return self._stop_requested.is_set()

    def _safe_run(self, fn, task) -> TransferResult:
        """중단 플래그 체크 후 워커 실행."""
        if self._stop_requested.is_set():
            return TransferResult(
                rel_path=task.rel_path,
                direction=task.direction,
                success=False,
                error="중단됨",
            )
        result = fn(task)
        # 완료 후에도 체크 (다음 파일 시작 안 하도록)
        return result

    # ──────────────────────────────────────────────
    # 스레드별 DriveClient 지연 생성
    # ──────────────────────────────────────────────
    def _get_drive(self) -> DriveClient:
        drive = getattr(self._tls, "drive", None)
        if drive is None:
            drive = DriveClient(
                net=self.cfg.network,
                credentials=self._credentials,          # 재사용 (OAuth 로딩 1회)
                path_cache=self._shared_path_cache,     # 공유
                path_cache_lock=self._path_cache_lock,
                performance=self.cfg.performance,
                acknowledge_abuse=getattr(self.cfg, "acknowledge_abuse", False),
            )
            self._tls.drive = drive
            log.debug(f"TLS DriveClient 생성: {threading.current_thread().name}")
        return drive

    # ──────────────────────────────────────────────
    # 실제 업/다운로드 (워커 스레드에서 실행)
    # ──────────────────────────────────────────────
    def _do_upload(self, task: "UploadTask") -> TransferResult:
        drive = self._get_drive()
        lf = task.local

        pbar = None
        if self.progress_factory:
            pbar = self.progress_factory(task.rel_path, lf.size, "↑")

        # 청크 progress 의 직전 값 — delta 계산용 (워치독 빠른 감지)
        last_current = [0]
        chunk_cb = self.chunk_byte_callback

        def cb(current, total):
            if pbar:
                try:
                    pbar.update(current - pbar.n)
                except Exception:
                    pass
            if chunk_cb:
                delta = current - last_current[0]
                last_current[0] = current
                if delta > 0:
                    try:
                        chunk_cb(delta)
                    except Exception:
                        pass

        # 업로드 직전 재확인 — 스캔과 업로드 사이에 사라진 파일은 '오류' 아닌 '소프트 스킵'
        # 안티바이러스/DLP 류가 짧게 만들었다 지우는 임시파일(예: 6자리 hex .docx)을 조용히 무시
        if not lf.abs_path.exists():
            if pbar:
                try:
                    pbar.close()
                except Exception:
                    pass
            # 평소엔 노이즈가 되므로 DEBUG로 — 요약에 카운트/샘플 파일명이 표시됨
            log.debug(
                f"업로드 스킵 (스캔 후 사라짐): {task.rel_path}  "
                f"— 외부 프로그램(백신/DLP 등)의 임시파일일 가능성"
            )
            return TransferResult(
                rel_path=task.rel_path,
                direction="upload",
                success=False,
                error="스캔 후 사라짐",
                is_conflict=task.is_conflict,
                vanished=True,
            )

        try:
            result = drive.upload_file(
                lf.abs_path,
                parent_id=task.parent_id,
                name=Path(task.rel_path).name,
                existing_id=task.existing_id,
                progress_cb=cb,
                bandwidth_limiter=self.bandwidth,
                strategy="auto",
                stop_checker=self.is_stop_requested,
            )
        except FileNotFoundError as e:
            # MediaFileUpload가 파일을 여는 시점에 사라진 경우 (위 체크와 업로드 사이의 미세 경합)
            if pbar:
                try:
                    pbar.close()
                except Exception:
                    pass
            log.debug(
                f"업로드 스킵 (전송 직전 사라짐): {task.rel_path}  — {e}"
            )
            return TransferResult(
                rel_path=task.rel_path,
                direction="upload",
                success=False,
                error="전송 직전 사라짐",
                is_conflict=task.is_conflict,
                vanished=True,
            )
        except OSError as e:
            # WinError 2 (파일 없음)는 FileNotFoundError로 잡히지만, 일부 경로에서
            # 일반 OSError로 올라올 수 있음 (winerror==2)
            winerr = getattr(e, "winerror", None)
            if winerr == 2 or "지정된 파일을 찾을 수 없습니다" in str(e):
                if pbar:
                    try:
                        pbar.close()
                    except Exception:
                        pass
                log.debug(
                    f"업로드 스킵 (파일 없음): {task.rel_path}  — {e}"
                )
                return TransferResult(
                    rel_path=task.rel_path,
                    direction="upload",
                    success=False,
                    error="파일 없음",
                    is_conflict=task.is_conflict,
                    vanished=True,
                )
            if pbar:
                try:
                    pbar.close()
                except Exception:
                    pass
            log.error(f"업로드 실패 {task.rel_path}: {e}")
            return TransferResult(
                rel_path=task.rel_path,
                direction="upload",
                success=False,
                error=str(e),
                is_conflict=task.is_conflict,
            )
        except Exception as e:
            if pbar:
                try:
                    pbar.close()
                except Exception:
                    pass
            log.error(f"업로드 실패 {task.rel_path}: {e}")
            return TransferResult(
                rel_path=task.rel_path,
                direction="upload",
                success=False,
                error=str(e),
                is_conflict=task.is_conflict,
            )

        if pbar:
            try:
                pbar.close()
            except Exception:
                pass

        file_state = FileState(
            local_size=lf.size,
            local_mtime=lf.mtime_iso,
            local_md5=lf._md5,
            remote_id=result.id,
            remote_size=result.size or lf.size,
            remote_mtime=result.modified_time,
            remote_md5=result.md5,
            mime_type=result.mime_type,
        )
        return TransferResult(
            rel_path=task.rel_path,
            direction="upload",
            success=True,
            file_state=file_state,
            bytes_done=lf.size,
            is_conflict=task.is_conflict,
        )

    def _do_download(self, task: "DownloadTask") -> TransferResult:
        drive = self._get_drive()
        rf = task.remote

        pbar = None
        if self.progress_factory:
            pbar = self.progress_factory(task.rel_path, rf.size, "↓")

        last_current = [0]
        chunk_cb = self.chunk_byte_callback

        def cb(current, total):
            if pbar:
                try:
                    pbar.update(current - pbar.n)
                except Exception:
                    pass
            if chunk_cb:
                delta = current - last_current[0]
                last_current[0] = current
                if delta > 0:
                    try:
                        chunk_cb(delta)
                    except Exception:
                        pass

        try:
            drive.download_file(
                rf.id,
                task.dest_path,
                progress_cb=cb,
                bandwidth_limiter=self.bandwidth,
            )
        except Exception as e:
            if pbar:
                try:
                    pbar.close()
                except Exception:
                    pass
            log.error(f"다운로드 실패 {task.rel_path}: {e}")
            return TransferResult(
                rel_path=task.rel_path,
                direction="download",
                success=False,
                error=str(e),
                is_conflict=task.is_conflict,
            )

        if pbar:
            try:
                pbar.close()
            except Exception:
                pass

        # Drive modifiedTime을 로컬 파일 mtime으로 보존
        # (다음 동기화에서 mtime 불일치로 인한 불필요한 재업로드 방지)
        if rf.modified_time:
            try:
                ts = parse_rfc3339(rf.modified_time).timestamp()
                os.utime(task.dest_path, (ts, ts))
            except Exception:
                pass

        st = task.dest_path.stat()
        file_state = FileState(
            local_size=st.st_size,
            local_mtime=mtime_to_iso(st.st_mtime),
            local_md5=None,
            remote_id=rf.id,
            remote_size=rf.size,
            remote_mtime=rf.modified_time,
            remote_md5=rf.md5,
            mime_type=rf.mime_type,
        )
        return TransferResult(
            rel_path=task.rel_path,
            direction="download",
            success=True,
            file_state=file_state,
            bytes_done=st.st_size,
            is_conflict=task.is_conflict,
        )


# ──────────────────────────────────────────────────────────
# 워커 태스크 정의
# ──────────────────────────────────────────────────────────

@dataclass
class UploadTask:
    rel_path: str
    local: LocalFile
    parent_id: str
    existing_id: Optional[str] = None
    is_conflict: bool = False

    @property
    def direction(self) -> str:
        return "upload"


@dataclass
class DownloadTask:
    rel_path: str
    remote: DriveFile
    dest_path: Path
    is_conflict: bool = False

    @property
    def direction(self) -> str:
        return "download"
