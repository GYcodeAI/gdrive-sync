"""ETACalculator 회귀 테스트."""

import time

from gdrive_sync.eta import ETACalculator


def test_warmup_returns_none():
    eta = ETACalculator(min_warmup_sec=5.0)
    eta.update(0)
    eta.update(1_000_000)
    # 워밍업 시간(5초) 이전이라 None 반환
    assert eta.rate_bps() is None
    assert eta.eta_seconds(10**8) is None
    assert "측정" in eta.format(10**8)


def test_rate_after_warmup():
    eta = ETACalculator(window_sec=30.0, min_warmup_sec=0.0)
    t0 = time.time()
    eta._samples.append((t0, 0))
    eta._samples.append((t0 + 10, 10_000_000))   # 10MB in 10s = 1 MB/s
    eta._start_time = t0
    rate = eta.rate_bps()
    assert rate is not None
    assert 900_000 < rate < 1_100_000   # 약 1MB/s


def test_eta_seconds():
    eta = ETACalculator(window_sec=30.0, min_warmup_sec=0.0)
    t0 = time.time()
    eta._samples.append((t0, 0))
    eta._samples.append((t0 + 10, 10_000_000))   # 1 MB/s
    eta._start_time = t0
    sec = eta.eta_seconds(10_000_000)   # 10MB / 1MB/s = 10s
    assert sec is not None
    assert 9 < sec < 11


def test_zero_remaining():
    eta = ETACalculator()
    assert eta.eta_seconds(0) == 0.0


def test_format_rate():
    eta = ETACalculator(min_warmup_sec=0.0)
    t0 = time.time()
    eta._samples.append((t0, 0))
    eta._samples.append((t0 + 1, 2_500_000))   # 2.5 MB/s
    eta._start_time = t0
    txt = eta.format_rate()
    assert "MB/s" in txt


def test_window_eviction():
    """오래된 샘플은 윈도우 밖으로 제거됨."""
    eta = ETACalculator(window_sec=5.0, min_warmup_sec=0.0)
    t0 = time.time()
    # 30초 전 샘플 추가 (윈도우 밖)
    eta._samples.append((t0 - 30, 0))
    eta._start_time = t0 - 30
    # 새 샘플 추가하면 오래된 것 제거됨
    eta.update(1_000_000)
    # 단일 샘플만 남으면 rate 측정 불가
    assert eta.rate_bps() is None
