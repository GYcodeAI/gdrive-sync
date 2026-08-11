"""bandwidth.consume_with_progress 회귀 테스트."""

import time

from gdrive_sync.bandwidth import TokenBucket


def test_consume_with_progress_calls_callback_periodically():
    """1초치 단위로 callback 이 호출되어야 함."""
    bucket = TokenBucket(rate_bytes_per_sec=1024 * 1024)   # 1 MB/s
    calls = []

    def cb(consumed, total):
        calls.append(consumed)

    # 3MB consume → 약 3번 호출 + 마지막 한 번 = 약 4회
    bucket.consume_with_progress(3 * 1024 * 1024, callback=cb)
    assert len(calls) >= 2
    assert calls[-1] == 3 * 1024 * 1024


def test_unlimited_calls_callback_once():
    """rate=0 (무제한) 일 땐 callback 즉시 한 번만."""
    bucket = TokenBucket(rate_bytes_per_sec=0)
    calls = []

    def cb(consumed, total):
        calls.append((consumed, total))

    bucket.consume_with_progress(1_000_000, callback=cb)
    assert calls == [(1_000_000, 1_000_000)]


def test_stop_checker_aborts():
    """stop_checker 가 True 반환하면 즉시 중단."""
    bucket = TokenBucket(rate_bytes_per_sec=1024 * 1024)   # 1 MB/s
    calls = []

    def cb(consumed, total):
        calls.append(consumed)

    # 첫 chunk 호출 후 즉시 중단
    state = {"called": 0}

    def stop():
        state["called"] += 1
        return state["called"] >= 2   # 두 번째 체크 시점에 중단

    start = time.time()
    bucket.consume_with_progress(
        100 * 1024 * 1024,    # 100MB → 원래 100초 걸려야 함
        callback=cb, stop_checker=stop,
    )
    elapsed = time.time() - start
    assert elapsed < 5   # 100초 안 기다리고 빨리 빠져나옴
