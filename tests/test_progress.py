"""ProgressTracker (B3+) 회귀 테스트."""

from pathlib import Path
from unittest.mock import patch

from gdrive_sync.config import (
    BandwidthConfig, Config, NetworkConfig, PerformanceConfig,
    SchedulerConfig, SyncPair, TrashConfig,
)
from gdrive_sync.progress import ProgressTracker
from gdrive_sync.state import FileState, SyncState


def _make_cfg(pairs):
    return Config(
        sync_pairs=pairs,
        exclude_patterns=[],
        network=NetworkConfig(),
        performance=PerformanceConfig(),
        bandwidth=BandwidthConfig(),
        scheduler=SchedulerConfig(),
        trash=TrashConfig(),
    )


def _empty_state():
    return SyncState(files={})


def _state_with(file_count: int, bytes_each: int = 1024) -> SyncState:
    files = {
        f"file_{i}.txt": FileState(
            local_size=bytes_each, local_mtime="2026-01-01T00:00:00Z",
            local_md5=None, remote_id=f"id_{i}", remote_size=bytes_each,
            remote_mtime="2026-01-01T00:00:00Z", remote_md5=None,
        )
        for i in range(file_count)
    }
    return SyncState(files=files)


def test_first_sync_no_state_uses_average():
    """state 없는 pair는 알려진 pair 평균값으로 채움."""
    pairs = [
        SyncPair(local_path=Path("/a"), remote_path="A"),
        SyncPair(local_path=Path("/b"), remote_path="B"),
    ]
    cfg = _make_cfg(pairs)

    def fake_load_state(p):
        if str(p) in ("/a", "\\a", "a"):
            return _state_with(100)
        return _empty_state()

    with patch("gdrive_sync.progress.load_state", side_effect=fake_load_state):
        tracker = ProgressTracker(cfg)
    snap = tracker.snapshot()
    # /a 는 100개, /b 는 평균값 100으로 채움 → 합 200
    assert snap.files_total == 200


def test_all_no_state_zero_denominator():
    """모든 pair가 첫 동기화면 분모 0."""
    pairs = [SyncPair(local_path=Path("/x"), remote_path="X")]
    cfg = _make_cfg(pairs)
    with patch("gdrive_sync.progress.load_state", return_value=_empty_state()):
        tracker = ProgressTracker(cfg)
    snap = tracker.snapshot()
    # files_done(0)과 max → 0
    assert snap.files_total == 0


def test_label_progression():
    """[추정] → [부분 확정] → [확정] 순서로 라벨 변화."""
    pairs = [
        SyncPair(local_path=Path("/a"), remote_path="A"),
        SyncPair(local_path=Path("/b"), remote_path="B"),
    ]
    cfg = _make_cfg(pairs)
    with patch("gdrive_sync.progress.load_state", return_value=_state_with(50)):
        tracker = ProgressTracker(cfg)
    assert tracker.snapshot().label == "[추정]"

    tracker.on_pair_scanned(pairs[0], files=60, bytes_total=60_000)
    assert "부분 확정" in tracker.snapshot().label

    tracker.on_pair_scanned(pairs[1], files=70, bytes_total=70_000)
    assert tracker.snapshot().label == "[확정]"


def test_overall_ratio_calculation():
    """전체 진행률 = 완료 폴더 비율 + 현재 폴더 부분 비율."""
    pairs = [SyncPair(local_path=Path(f"/p{i}"), remote_path=f"P{i}") for i in range(4)]
    cfg = _make_cfg(pairs)
    with patch("gdrive_sync.progress.load_state", return_value=_state_with(100)):
        tracker = ProgressTracker(cfg)

    # 4폴더 중 1폴더 완료 = 0.25
    tracker.on_pair_done()
    snap = tracker.snapshot()
    assert abs(snap.overall_ratio - 0.25) < 0.01

    # 2폴더 완료 + 현재 50% = 0.5 + 0.125 = 0.625
    tracker.on_pair_done()
    tracker.on_pair_start(pairs[2])
    tracker.on_pair_scanned(pairs[2], files=10, bytes_total=10_000)
    for _ in range(5):
        tracker.on_file_done(bytes_delta=1000)
    snap = tracker.snapshot()
    assert abs(snap.overall_ratio - 0.625) < 0.05


def test_files_done_monotone():
    """on_file_done 호출 시 files_done 증가."""
    pairs = [SyncPair(local_path=Path("/p"), remote_path="P")]
    cfg = _make_cfg(pairs)
    with patch("gdrive_sync.progress.load_state", return_value=_state_with(10)):
        tracker = ProgressTracker(cfg)
    tracker.on_pair_start(pairs[0])
    tracker.on_pair_scanned(pairs[0], files=10, bytes_total=10_000)
    for _ in range(5):
        tracker.on_file_done(bytes_delta=100)
    snap = tracker.snapshot()
    assert snap.files_done == 5
    assert snap.bytes_done == 500
