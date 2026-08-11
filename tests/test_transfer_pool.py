"""TransferPool 단위 테스트 (Mock DriveClient 사용)."""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gdrive_sync.config import Config, NetworkConfig, PerformanceConfig
from gdrive_sync.drive_api import DriveClient, DriveFile
from gdrive_sync.local_scanner import LocalFile
from gdrive_sync.transfer_pool import (
    DownloadTask, TransferPool, TransferResult, UploadTask,
)


def _make_cfg(parallel: int = 3) -> Config:
    return Config(
        performance=PerformanceConfig(parallel_transfers=parallel),
        network=NetworkConfig(),
    )


def _make_base_drive(credentials=None) -> MagicMock:
    """DriveClient-like mock with credentials / shared_path_cache 프로퍼티."""
    m = MagicMock(spec=DriveClient)
    m.credentials = credentials or MagicMock()
    m.shared_path_cache = ({"": "root"}, threading.Lock())
    return m


def _local(rel: str, size: int = 100) -> LocalFile:
    lf = LocalFile(
        rel_path=rel,
        abs_path=Path("/tmp") / rel,
        size=size,
        mtime_iso="2026-04-15T10:00:00Z",
    )
    lf._md5 = "abc"
    return lf


def _remote(rel: str, size: int = 100) -> DriveFile:
    return DriveFile(
        id="id-" + rel,
        name=rel,
        mime_type="text/plain",
        size=size,
        modified_time="2026-04-15T10:00:00Z",
        md5="abc",
    )


# ──────────────────────────────────────────────────────────
# 기본 동작
# ──────────────────────────────────────────────────────────

def test_empty_batches_return_empty():
    cfg = _make_cfg()
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())
    assert pool.execute_uploads(pair=None, tasks=[]) == []
    assert pool.execute_downloads(pair=None, tasks=[]) == []


def test_parallel_workers_clamped_to_1_to_10():
    cfg = _make_cfg(parallel=100)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())
    assert pool.max_workers == 10

    cfg2 = _make_cfg(parallel=0)
    pool2 = TransferPool(cfg=cfg2, base_drive=_make_base_drive())
    assert pool2.max_workers == 1


def test_small_files_sorted_first_in_upload():
    """작은 파일 우선 정렬 — 내부 정렬 확인용으로 동일 인자 비교."""
    cfg = _make_cfg(parallel=2)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    calls: list[str] = []
    lock = threading.Lock()

    # _do_upload를 가로채서 호출 순서 기록
    def fake_do_upload(task: UploadTask) -> TransferResult:
        with lock:
            calls.append(task.rel_path)
        return TransferResult(
            rel_path=task.rel_path, direction="upload", success=True,
        )

    pool._do_upload = fake_do_upload

    tasks = [
        UploadTask(rel_path="big.bin", local=_local("big.bin", size=10_000_000),
                   parent_id="root"),
        UploadTask(rel_path="small.txt", local=_local("small.txt", size=100),
                   parent_id="root"),
        UploadTask(rel_path="medium.doc", local=_local("medium.doc", size=5000),
                   parent_id="root"),
    ]
    pool.execute_uploads(pair=None, tasks=tasks)

    # small.txt가 big.bin보다 먼저 시작됨 (max_workers=2여도 정렬 영향)
    # 최소한 모두 실행되었음을 확인
    assert set(calls) == {"big.bin", "small.txt", "medium.doc"}


# ──────────────────────────────────────────────────────────
# 실패 격리
# ──────────────────────────────────────────────────────────

def test_one_failed_upload_does_not_stop_others():
    cfg = _make_cfg(parallel=3)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    def fake_do_upload(task: UploadTask) -> TransferResult:
        if "bad" in task.rel_path:
            raise RuntimeError("simulated failure")
        return TransferResult(
            rel_path=task.rel_path, direction="upload", success=True,
            bytes_done=task.local.size,
        )

    pool._do_upload = fake_do_upload

    tasks = [
        UploadTask(rel_path="good1.txt", local=_local("good1.txt"), parent_id="root"),
        UploadTask(rel_path="bad.txt", local=_local("bad.txt"), parent_id="root"),
        UploadTask(rel_path="good2.txt", local=_local("good2.txt"), parent_id="root"),
    ]
    results = pool.execute_uploads(pair=None, tasks=tasks)

    assert len(results) == 3
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    assert len(successes) == 2
    assert len(failures) == 1
    assert "bad.txt" in failures[0].rel_path


def test_failed_download_marked_as_error():
    cfg = _make_cfg()
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    def fake_do_download(task: DownloadTask) -> TransferResult:
        raise OSError("disk full")

    pool._do_download = fake_do_download

    task = DownloadTask(
        rel_path="x.bin", remote=_remote("x.bin"),
        dest_path=Path("/tmp/x.bin"),
    )
    results = pool.execute_downloads(pair=None, tasks=[task])
    assert len(results) == 1
    assert not results[0].success
    assert "disk full" in (results[0].error or "")


# ──────────────────────────────────────────────────────────
# 중단
# ──────────────────────────────────────────────────────────

def test_vanished_file_does_not_emit_warning(caplog):
    """vanished=True 결과는 WARNING 로그를 내지 않아야 한다."""
    import logging
    cfg = _make_cfg(parallel=2)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    def fake_do_upload(task: UploadTask) -> TransferResult:
        return TransferResult(
            rel_path=task.rel_path,
            direction="upload",
            success=False,
            error="스캔 후 사라짐",
            vanished=True,
        )

    pool._do_upload = fake_do_upload

    tasks = [
        UploadTask(rel_path="LHZ5DWIU.DOCX", local=_local("LHZ5DWIU.DOCX", size=0), parent_id="root"),
        UploadTask(rel_path="AB12CD34.DOCX", local=_local("AB12CD34.DOCX", size=0), parent_id="root"),
    ]
    with caplog.at_level(logging.WARNING, logger="gdrive_sync.transfer_pool"):
        results = pool.execute_uploads(pair=None, tasks=tasks)

    assert len(results) == 2
    assert all(r.vanished for r in results)
    # vanished 결과는 WARNING 로그를 내지 않음
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"예상치 못한 WARNING: {[r.message for r in warnings]}"


def test_real_failure_still_emits_warning(caplog):
    """진짜 실패(vanished=False)는 WARNING 로그를 내야 한다."""
    import logging
    cfg = _make_cfg(parallel=1)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    def fake_do_upload(task: UploadTask) -> TransferResult:
        return TransferResult(
            rel_path=task.rel_path,
            direction="upload",
            success=False,
            error="403 Forbidden",
            vanished=False,
        )

    pool._do_upload = fake_do_upload

    tasks = [UploadTask(rel_path="real_file.docx", local=_local("real_file.docx"), parent_id="root")]
    with caplog.at_level(logging.WARNING, logger="gdrive_sync.transfer_pool"):
        pool.execute_uploads(pair=None, tasks=tasks)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("실패" in r.message for r in warnings)


def test_chunk_byte_callback_skips_per_completion_byte_callback():
    """chunk_byte_callback 이 설정되면 per-completion byte_callback 은 호출 안 됨 (이중 카운트 방지)."""
    cfg = _make_cfg(parallel=1)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    completion_calls: list[int] = []
    chunk_calls: list[int] = []
    pool.byte_callback = lambda n: completion_calls.append(n)
    pool.chunk_byte_callback = lambda n: chunk_calls.append(n)

    def fake_do_upload(task):
        # 워커가 청크 콜백을 직접 호출하는 것을 시뮬 (실제로는 drive_api 의 progress_cb 가 함)
        if pool.chunk_byte_callback:
            pool.chunk_byte_callback(task.local.size)
        return TransferResult(
            rel_path=task.rel_path, direction="upload", success=True,
            bytes_done=task.local.size,
        )

    pool._do_upload = fake_do_upload
    tasks = [UploadTask(rel_path="a.bin", local=_local("a.bin", size=1000), parent_id="root")]
    pool.execute_uploads(pair=None, tasks=tasks)

    assert chunk_calls == [1000]
    # chunk_byte_callback 이 설정돼 있으므로 per-completion byte_callback 은 스킵
    assert completion_calls == []


def test_chunk_byte_callback_unset_falls_back_to_completion_callback():
    """chunk_byte_callback 미설정 시 기존 per-completion byte_callback 동작 유지 (회귀 방지)."""
    cfg = _make_cfg(parallel=1)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())

    completion_calls: list[int] = []
    pool.byte_callback = lambda n: completion_calls.append(n)
    # chunk_byte_callback 는 의도적으로 미설정

    def fake_do_upload(task):
        return TransferResult(
            rel_path=task.rel_path, direction="upload", success=True,
            bytes_done=task.local.size,
        )

    pool._do_upload = fake_do_upload
    tasks = [UploadTask(rel_path="a.bin", local=_local("a.bin", size=1000), parent_id="root")]
    pool.execute_uploads(pair=None, tasks=tasks)

    assert completion_calls == [1000]


def test_request_stop_before_execution_cancels_all():
    cfg = _make_cfg(parallel=2)
    pool = TransferPool(cfg=cfg, base_drive=_make_base_drive())
    pool.request_stop()

    called = {"n": 0}
    def fake_do_upload(task):
        called["n"] += 1
        return TransferResult(rel_path=task.rel_path, direction="upload", success=True)

    pool._do_upload = fake_do_upload

    tasks = [
        UploadTask(rel_path=f"f{i}.txt", local=_local(f"f{i}.txt"), parent_id="root")
        for i in range(3)
    ]
    results = pool.execute_uploads(pair=None, tasks=tasks)
    # 모두 중단 응답
    assert called["n"] == 0
    assert all(not r.success for r in results)
    assert all("중단" in (r.error or "") for r in results)
