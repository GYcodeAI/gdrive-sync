"""3단계 중단 회귀 테스트."""

from unittest.mock import MagicMock

from gdrive_sync.config import (
    BandwidthConfig, Config, NetworkConfig, PerformanceConfig,
    SchedulerConfig, TrashConfig,
)
from gdrive_sync.sync_engine import SyncEngine


def _make_engine():
    cfg = Config(
        sync_pairs=[],
        exclude_patterns=[],
        network=NetworkConfig(),
        performance=PerformanceConfig(),
        bandwidth=BandwidthConfig(),
        scheduler=SchedulerConfig(),
        trash=TrashConfig(),
    )
    return SyncEngine(cfg=cfg, drive=MagicMock(), dry_run=True)


def test_default_no_stop():
    e = _make_engine()
    assert not e.is_stop_after_file_requested()
    assert not e.is_stop_after_pair_requested()
    assert not e.is_force_stop_requested()


def test_stop_after_file_only():
    e = _make_engine()
    e.request_stop_after_file()
    assert e.is_stop_after_file_requested()
    # file-level stop 은 pair-level 트리거 안 함
    assert not e.is_stop_after_pair_requested()
    assert not e.is_force_stop_requested()


def test_stop_after_pair_implies_file():
    """폴더 후 중단을 요청하면 파일 후도 자동으로 True (상위 → 하위 포함)."""
    e = _make_engine()
    e.request_stop_after_pair()
    assert e.is_stop_after_pair_requested()
    # 폴더 단위 중단은 파일 단위 효과도 포함 (다음 폴더 시작 전 체크에서 둘 다 막음)
    # is_stop_after_file_requested 자체는 pair-level set만으로도 True가 됨
    assert e.is_stop_after_file_requested()
    assert not e.is_force_stop_requested()


def test_force_stop_implies_all():
    """강제 중단은 모든 하위 단계 트리거."""
    e = _make_engine()
    e.request_force_stop()
    assert e.is_force_stop_requested()
    assert e.is_stop_after_pair_requested()
    assert e.is_stop_after_file_requested()


def test_legacy_request_stop_maps_to_file_level():
    """기존 request_stop()은 파일 단위 중단으로 매핑."""
    e = _make_engine()
    e.request_stop()
    assert e.is_stop_after_file_requested()
    # 폴더 단위까지 트리거하지 않음
    assert not e.is_stop_after_pair_requested()
    assert not e.is_force_stop_requested()


def test_escalation_only():
    """상위 단계로 격상은 가능, 하위로 내려갈 수 없음."""
    e = _make_engine()
    e.request_stop_after_file()
    e.request_force_stop()   # 격상
    assert e.is_force_stop_requested()
    # 한 번 set된 이벤트는 clear 안 됨 (의도된 단방향)
    assert e.is_stop_after_file_requested()
    assert e.is_stop_after_pair_requested()
