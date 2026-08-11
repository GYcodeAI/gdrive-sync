"""StateWriter 비동기 쓰기 테스트 (D)."""

from pathlib import Path

from gdrive_sync.state import (
    FileState, StateWriter, SyncState, load_state, save_state, state_path_for,
)


def _make_state(n: int = 3) -> SyncState:
    return SyncState(
        last_sync="2026-05-12T00:00:00Z",
        files={
            f"f{i}.txt": FileState(
                local_size=100 + i, local_mtime="2026-05-12T00:00:00Z",
                local_md5=f"m{i}", remote_id=f"id{i}",
                remote_size=100 + i, remote_mtime="2026-05-12T00:00:00Z",
                remote_md5=f"m{i}",
            )
            for i in range(n)
        },
    )


def test_writer_enqueue_persists_on_shutdown(tmp_path: Path):
    writer = StateWriter()
    writer.start()
    try:
        writer.enqueue(tmp_path, _make_state(5))
    finally:
        writer.shutdown(wait=True)

    loaded = load_state(tmp_path)
    assert len(loaded.files) == 5
    assert loaded.files["f3.txt"].local_md5 == "m3"


def test_writer_multiple_paths(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    writer = StateWriter()
    writer.start()
    try:
        writer.enqueue(a, _make_state(2))
        writer.enqueue(b, _make_state(4))
        writer.flush()
    finally:
        writer.shutdown(wait=True)

    assert len(load_state(a).files) == 2
    assert len(load_state(b).files) == 4


def test_writer_fallback_when_not_started(tmp_path: Path):
    """start() 호출 안 하면 동기 fallback — 즉시 디스크에 씀."""
    writer = StateWriter()
    writer.enqueue(tmp_path, _make_state(1))
    # start 안 했으니 동기적으로 저장됐어야 함
    assert state_path_for(tmp_path).exists()


def test_writer_shutdown_idempotent(tmp_path: Path):
    writer = StateWriter()
    writer.start()
    writer.shutdown(wait=True)
    writer.shutdown(wait=True)   # 두 번째 호출은 무해


def test_writer_flush_waits_for_pending(tmp_path: Path):
    writer = StateWriter()
    writer.start()
    try:
        for i in range(3):
            sub = tmp_path / f"d{i}"
            sub.mkdir()
            writer.enqueue(sub, _make_state(2))
        writer.flush()
        # flush 직후 모든 파일이 디스크에 있어야 함
        for i in range(3):
            assert state_path_for(tmp_path / f"d{i}").exists()
    finally:
        writer.shutdown(wait=True)
