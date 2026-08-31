"""GUI 무진행 워치독 (B) 바이트-트리클 판정 회귀 테스트.

SyncApp._check_bytes_stall 은 self 의 _progress_tracker / _bytes_baseline* 과
클래스 상수만 쓰므로 Tk 창 없이 스텁 self 로 unbound 호출해 검증한다.

배경(v2.4.3 오탐): 앞 폴더에서 바이트가 한 번 발생한 뒤 대형 폴더 스캔이 2분
넘게 이어지자 "2분간 바이트 진척 315 KB (트리클)"로 강제 중단됨. 전송 단계
(transfer_active)에서만 평가하도록 수정.
"""

import pytest

pytest.importorskip("tkinter")

from gdrive_sync.gui import SyncApp  # noqa: E402


class _Snap:
    def __init__(self, bytes_done=0, transfer_active=False, transfer_remaining_bytes=0):
        self.bytes_done = bytes_done
        self.transfer_active = transfer_active
        self.transfer_remaining_bytes = transfer_remaining_bytes


class _Tracker:
    def __init__(self):
        self.snap = _Snap()

    def snapshot(self):
        return self.snap


class _Stub:
    """SyncApp 인스턴스 대역 — 워치독이 참조하는 속성만 보유."""
    BYTES_STALL_THRESHOLD_SEC = SyncApp.BYTES_STALL_THRESHOLD_SEC
    BYTES_STALL_MIN_DELTA = SyncApp.BYTES_STALL_MIN_DELTA

    def __init__(self):
        self._progress_tracker = _Tracker()
        self._bytes_baseline_ts = None
        self._bytes_baseline = 0


def _check(stub, now):
    return SyncApp._check_bytes_stall(stub, now)


T = SyncApp.BYTES_STALL_THRESHOLD_SEC
MB = 1024 * 1024


def test_scan_phase_after_earlier_bytes_does_not_trigger():
    """v2.4.3 오탐 재현: 앞 폴더 전송 후 스캔 단계가 2분 넘게 이어져도 미발동."""
    s = _Stub()
    # 앞 폴더 전송 중 — 베이스라인 설정
    s._progress_tracker.snap = _Snap(bytes_done=3 * MB, transfer_active=True,
                                     transfer_remaining_bytes=50 * MB)
    assert _check(s, 0.0) is None
    assert s._bytes_baseline_ts == 0.0
    # 전송 끝나고 다음 폴더 스캔 단계 진입 (바이트 안 움직임)
    s._progress_tracker.snap = _Snap(bytes_done=3 * MB + 315 * 1024, transfer_active=False)
    assert _check(s, 10.0) is None
    assert s._bytes_baseline_ts is None          # 베이스라인 비워짐
    assert _check(s, T + 60.0) is None           # 2분 훨씬 넘어도 미발동


def test_transfer_stall_triggers():
    """전송 단계에서 실제로 바이트가 안 움직이면 임계 후 발동."""
    s = _Stub()
    s._progress_tracker.snap = _Snap(bytes_done=10 * MB, transfer_active=True,
                                     transfer_remaining_bytes=200 * MB)
    assert _check(s, 0.0) is None                # 베이스라인
    assert _check(s, T - 1.0) is None            # 아직 임계 전
    reason = _check(s, T + 0.5)
    assert reason is not None
    assert "트리클" in reason


def test_transfer_progress_resets_baseline():
    """임계 이상 진척하면 베이스라인 갱신 → 미발동."""
    s = _Stub()
    s._progress_tracker.snap = _Snap(bytes_done=0, transfer_active=True,
                                     transfer_remaining_bytes=200 * MB)
    assert _check(s, 0.0) is None
    s._progress_tracker.snap = _Snap(bytes_done=1 * MB, transfer_active=True,
                                     transfer_remaining_bytes=199 * MB)
    assert _check(s, T - 10.0) is None
    assert s._bytes_baseline_ts == T - 10.0
    assert _check(s, T + 10.0) is None           # 리셋 후 20초밖에 안 지남


def test_small_remaining_batch_does_not_trigger():
    """남은 양이 임계(512KB) 미만이면 느려도 판정 불가 → 미발동."""
    s = _Stub()
    s._progress_tracker.snap = _Snap(bytes_done=1 * MB, transfer_active=True,
                                     transfer_remaining_bytes=100 * 1024)
    assert _check(s, 0.0) is None
    assert s._bytes_baseline_ts is None
    assert _check(s, T + 60.0) is None


def test_new_batch_starts_fresh_baseline():
    """스캔으로 베이스라인이 비워진 뒤 새 배치 시작 시 그 시점부터 다시 잰다."""
    s = _Stub()
    s._progress_tracker.snap = _Snap(bytes_done=5 * MB, transfer_active=False)
    assert _check(s, 0.0) is None
    s._progress_tracker.snap = _Snap(bytes_done=5 * MB, transfer_active=True,
                                     transfer_remaining_bytes=100 * MB)
    assert _check(s, 500.0) is None
    assert s._bytes_baseline_ts == 500.0
    assert _check(s, 500.0 + T - 1) is None
    assert _check(s, 500.0 + T + 1) is not None


# ──────────────────────────────────────────────────────────────────
# v2.4.5 추가: _poll_queue / _watchdog_loop 생존 보장 + 워치독 게이트
# (worker_thread 생존 기준) + 블랙박스 스택 덤프 + 로그 배치 BMP 안전 삽입
# ──────────────────────────────────────────────────────────────────

import logging
import time
import tkinter as tk
import types


class _RootStub:
    """self.root 대역 — after() 호출만 기록."""

    def __init__(self):
        self.after_calls: list[tuple[int, object]] = []

    def after(self, ms, fn):
        self.after_calls.append((ms, fn))


def test_poll_queue_swallows_exception_and_reschedules():
    """_poll_queue_once 가 예외를 던져도 전파되지 않고 다음 폴링이 재예약된다."""
    stub = types.SimpleNamespace()
    stub.root = _RootStub()
    stub.QUEUE_POLL_MS = SyncApp.QUEUE_POLL_MS
    stub._poll_queue = "sentinel-bound-method"  # after() 두 번째 인자로 참조될 자리

    def _boom():
        raise RuntimeError("poll boom")

    stub._poll_queue_once = _boom

    SyncApp._poll_queue(stub)  # 예외가 여기서 새어나오면 테스트 실패

    assert stub.root.after_calls == [(SyncApp.QUEUE_POLL_MS, stub._poll_queue)]


def test_watchdog_loop_swallows_exception_and_reschedules():
    """_check_no_progress_watchdog 가 예외를 던져도 전파되지 않고 재예약된다."""
    stub = types.SimpleNamespace()
    stub.root = _RootStub()
    stub.WATCHDOG_POLL_MS = SyncApp.WATCHDOG_POLL_MS
    stub._watchdog_loop = "sentinel-bound-method"

    def _boom():
        raise RuntimeError("watchdog boom")

    stub._check_no_progress_watchdog = _boom

    SyncApp._watchdog_loop(stub)

    assert stub.root.after_calls == [(SyncApp.WATCHDOG_POLL_MS, stub._watchdog_loop)]


class _WorkerStub:
    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self):
        return self._alive


class _WatchdogGateStub:
    """_check_no_progress_watchdog 이 참조하는 속성만 갖춘 대역.

    (기존 테스트가 sync_btn 등 버튼 상태를 목킹하지 않으므로 수정 불필요 —
    게이트는 이 파일에서 처음 다루는 대상이라 충돌 없음.)
    """

    BYTES_STALL_THRESHOLD_SEC = SyncApp.BYTES_STALL_THRESHOLD_SEC
    BYTES_STALL_MIN_DELTA = SyncApp.BYTES_STALL_MIN_DELTA
    NO_PROGRESS_THRESHOLD_SEC = SyncApp.NO_PROGRESS_THRESHOLD_SEC

    def __init__(self, worker_thread=None, last_activity_ts=None):
        self.root = _RootStub()
        self.worker_thread = worker_thread
        self.current_engine = None
        self._watchdog_triggered = False
        self._last_activity_ts = last_activity_ts
        self._progress_tracker = _Tracker()
        self._bytes_baseline_ts = None
        self._bytes_baseline = 0
        self._watchdog_finalize = "sentinel"
        self.logged: list[tuple[str, str]] = []

    def _log(self, msg, level="INFO"):
        self.logged.append((level, msg))

    def _dump_thread_stacks(self, reason):
        # 별도 테스트(test_dump_thread_stacks_logs_stack_dump)에서 실제 동작 검증 —
        # 여기서는 게이트/발동 로직만 확인하므로 no-op.
        pass


def test_watchdog_gate_no_worker_thread_does_not_trigger():
    """worker_thread 가 None 이면 (활동 정지 30분 초과라도) 미발동."""
    stub = _WatchdogGateStub(
        worker_thread=None,
        last_activity_ts=time.monotonic() - 31 * 60,
    )
    SyncApp._check_no_progress_watchdog(stub)
    assert stub._watchdog_triggered is False


def test_watchdog_gate_dead_worker_thread_does_not_trigger():
    """worker_thread.is_alive() 가 False 면 미발동."""
    stub = _WatchdogGateStub(
        worker_thread=_WorkerStub(alive=False),
        last_activity_ts=time.monotonic() - 31 * 60,
    )
    SyncApp._check_no_progress_watchdog(stub)
    assert stub._watchdog_triggered is False


def test_watchdog_gate_alive_worker_triggers_after_threshold():
    """alive 워커 + 활동 정지 31분 → 발동(_watchdog_triggered True)."""
    stub = _WatchdogGateStub(
        worker_thread=_WorkerStub(alive=True),
        last_activity_ts=time.monotonic() - 31 * 60,
    )
    SyncApp._check_no_progress_watchdog(stub)
    assert stub._watchdog_triggered is True
    assert any("진행 신호 없음" in msg for _level, msg in stub.logged)


def test_dump_thread_stacks_logs_stack_dump(caplog):
    """예외 없이 실행되고 로그에 '스레드 스택 덤프' 문자열이 남는다."""
    caplog.set_level(logging.ERROR, logger="gdrive_sync.gui")

    SyncApp._dump_thread_stacks(None, "테스트 사유")  # self 미사용이라 None 가능

    assert any("스레드 스택 덤프" in r.message for r in caplog.records)
    assert any(r.name == "gdrive_sync.gui" for r in caplog.records)


class _FakeLogText:
    """log_text 대역 — insert 는 필요 시 첫 호출에서 TclError 를 던지도록 설정."""

    def __init__(self, raise_on_insert=False, raise_always=False):
        self.config_calls: list[dict] = []
        self.insert_calls: list[tuple[str, str, str]] = []
        self._raise_on_insert = raise_on_insert
        self._raise_always = raise_always
        self._insert_attempts = 0

    def config(self, **kwargs):
        self.config_calls.append(kwargs)

    def insert(self, pos, text, tag):
        self._insert_attempts += 1
        should_raise = self._raise_always or (
            self._raise_on_insert and self._insert_attempts == 1
        )
        if should_raise:
            raise tk.TclError("simulated BMP-outside-plane failure")
        self.insert_calls.append((pos, text, tag))

    def index(self, _spec):
        return "5.0"

    def delete(self, _start, _end):
        pass

    def see(self, _pos):
        pass


def test_log_batch_retries_with_safe_string_after_tclerror():
    """log_text.insert 가 첫 호출에서 TclError → 치환된 문자열로 재시도."""
    stub = types.SimpleNamespace()
    stub.log_text = _FakeLogText(raise_on_insert=True)
    stub._MAX_LOG_LINES = SyncApp._MAX_LOG_LINES

    msg = "BMP 밖 문자 포함\U0001F600 메시지"
    SyncApp._log_batch(stub, [("INFO", msg)])

    assert len(stub.log_text.insert_calls) == 1
    _pos, retried_text, _tag = stub.log_text.insert_calls[0]
    assert "\U0001F600" not in retried_text
    assert "□" in retried_text
    assert stub.log_text.config_calls[-1] == {"state": "disabled"}


def test_log_batch_disables_widget_even_if_retry_also_raises():
    """재시도 insert 마저 예외를 던져도 마지막엔 config(state='disabled') 호출."""
    stub = types.SimpleNamespace()
    stub.log_text = _FakeLogText(raise_always=True)
    stub._MAX_LOG_LINES = SyncApp._MAX_LOG_LINES

    with pytest.raises(tk.TclError):
        SyncApp._log_batch(stub, [("INFO", "plain ascii message")])

    assert stub.log_text.config_calls[-1] == {"state": "disabled"}
