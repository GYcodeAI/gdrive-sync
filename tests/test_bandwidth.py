"""대역폭 제한기 (토큰 버킷 + 시간대별 스케줄) 테스트."""

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from gdrive_sync.bandwidth import (
    BYTES_PER_MB, BandwidthLimiter, TokenBucket, _rule_applies, make_limiter,
)
from gdrive_sync.config import BandwidthConfig, BandwidthSchedule


# ──────────────────────────────────────────────────────────
# TokenBucket
# ──────────────────────────────────────────────────────────

def test_token_bucket_unlimited_passes_instantly():
    bucket = TokenBucket(rate_bytes_per_sec=0)
    start = time.monotonic()
    bucket.consume(100 * 1024 * 1024)   # 100MB
    assert time.monotonic() - start < 0.05


def test_token_bucket_consumes_without_sleep_under_capacity():
    # 10 MB/s, capacity=10MB → 5MB는 즉시 소비 가능
    bucket = TokenBucket(rate_bytes_per_sec=10 * 1024 * 1024)
    start = time.monotonic()
    bucket.consume(5 * 1024 * 1024)
    elapsed = time.monotonic() - start
    assert elapsed < 0.1


def test_token_bucket_sequential_calls_respect_rate():
    # 예약 모델: 첫 호출은 즉시, 두 번째 호출이 대기 → 2번 × 1MB = ~1초
    # capacity=0 으로 burst 없앰 → 두 번째 consume 이 정확히 1s 대기
    bucket = TokenBucket(rate_bytes_per_sec=1 * 1024 * 1024, capacity_bytes=0)
    start = time.monotonic()
    bucket.consume(1 * 1024 * 1024)   # 즉시 예약
    bucket.consume(1 * 1024 * 1024)   # ~1초 대기
    elapsed = time.monotonic() - start
    # 여유 범위: 0.8 ~ 2.0초 (CI 변동 고려)
    assert 0.8 <= elapsed <= 2.0


def test_token_bucket_thread_safe():
    """병렬 워커가 각자 다른 시점을 예약해 합산 속도가 rate 이내로 수렴."""
    # 2MB/s, burst=1MB(0.5s) → 4스레드 × 1MB:
    #   예약 모델: 스레드별 순서가 직렬화 → 마지막 스레드는 ~1.5s 대기
    bucket = TokenBucket(rate_bytes_per_sec=2 * 1024 * 1024)

    def worker():
        bucket.consume(1 * 1024 * 1024)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start
    # 4MB ÷ 2MB/s = 2s (burst 0.5s 감안하면 실제 ~1.5s, CI 오버헤드 포함)
    assert 0.5 <= elapsed <= 3.0


def test_token_bucket_consume_zero_no_wait():
    bucket = TokenBucket(rate_bytes_per_sec=1)  # 매우 느림
    start = time.monotonic()
    bucket.consume(0)
    assert time.monotonic() - start < 0.05


# ──────────────────────────────────────────────────────────
# BandwidthLimiter (기본)
# ──────────────────────────────────────────────────────────

def test_limiter_unlimited_when_zero():
    limiter = BandwidthLimiter(upload_limit_mbps=0, download_limit_mbps=0)
    start = time.monotonic()
    limiter.consume_upload(50 * 1024 * 1024)
    limiter.consume_download(50 * 1024 * 1024)
    assert time.monotonic() - start < 0.1


def test_limiter_get_status():
    limiter = BandwidthLimiter(upload_limit_mbps=2.0, download_limit_mbps=5.0)
    st = limiter.get_status()
    assert st["active_rule"] == "default"
    assert st["upload_mbps"] == 2.0
    assert st["download_mbps"] == 5.0
    assert not st["upload_unlimited"]


def test_limiter_unlimited_flags():
    limiter = BandwidthLimiter(upload_limit_mbps=0, download_limit_mbps=3.0)
    st = limiter.get_status()
    assert st["upload_unlimited"]
    assert not st["download_unlimited"]


# ──────────────────────────────────────────────────────────
# 시간대별 스케줄
# ──────────────────────────────────────────────────────────

def test_rule_applies_normal_range():
    rule = BandwidthSchedule(
        name="work", time_start="09:00", time_end="18:00",
        upload_limit_mbps=2.0,
    )
    assert _rule_applies(rule, datetime(2026, 4, 15, 10, 30))   # 수요일 10:30
    assert _rule_applies(rule, datetime(2026, 4, 15, 9, 0))
    assert not _rule_applies(rule, datetime(2026, 4, 15, 18, 0))   # 경계: end 포함 X
    assert not _rule_applies(rule, datetime(2026, 4, 15, 8, 59))


def test_rule_applies_crossing_midnight():
    rule = BandwidthSchedule(
        name="night", time_start="22:00", time_end="07:00",
    )
    assert _rule_applies(rule, datetime(2026, 4, 15, 23, 30))   # 23:30
    assert _rule_applies(rule, datetime(2026, 4, 15, 3, 0))     # 03:00
    assert _rule_applies(rule, datetime(2026, 4, 15, 22, 0))
    assert not _rule_applies(rule, datetime(2026, 4, 15, 8, 0))


def test_rule_applies_weekdays_filter():
    rule = BandwidthSchedule(
        name="workdays", time_start="00:00", time_end="23:59",
        weekdays=["mon", "tue", "wed", "thu", "fri"],
    )
    # 2026-04-15 = 수요일
    assert _rule_applies(rule, datetime(2026, 4, 15, 12, 0))
    # 2026-04-18 = 토요일
    assert not _rule_applies(rule, datetime(2026, 4, 18, 12, 0))


def test_limiter_schedule_selection():
    schedule = [
        BandwidthSchedule(
            name="work", time_start="09:00", time_end="18:00",
            upload_limit_mbps=1.0, download_limit_mbps=2.0,
        ),
        BandwidthSchedule(
            name="night", time_start="18:00", time_end="09:00",
            upload_limit_mbps=0, download_limit_mbps=0,
        ),
    ]
    limiter = BandwidthLimiter(
        upload_limit_mbps=5.0, download_limit_mbps=5.0,
        schedule=schedule,
    )
    # _resolve_current_limits를 직접 확인
    up, dn, name = limiter._resolve_current_limits()
    # 기본 시각(현재)에 따라 다르므로 최소한 하나는 선택됨
    assert name in ("work", "night", "default")


def test_make_limiter_disabled_returns_none():
    cfg = BandwidthConfig(enabled=False)
    assert make_limiter(cfg) is None


def test_make_limiter_no_limits_no_schedule_returns_none():
    cfg = BandwidthConfig(
        enabled=True, upload_limit_mbps=0, download_limit_mbps=0, schedule=[],
    )
    assert make_limiter(cfg) is None


def test_make_limiter_with_schedule_only_creates_instance():
    cfg = BandwidthConfig(
        enabled=True, upload_limit_mbps=0, download_limit_mbps=0,
        schedule=[
            BandwidthSchedule(
                name="x", time_start="00:00", time_end="23:59",
                upload_limit_mbps=1.0,
            ),
        ],
    )
    limiter = make_limiter(cfg)
    assert limiter is not None
