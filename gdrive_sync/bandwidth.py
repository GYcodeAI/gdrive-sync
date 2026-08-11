"""토큰 버킷 기반 대역폭 제한기.

동작 원리:
- 버킷 하나가 일정 속도(bytes/sec)로 토큰이 채워짐
- 데이터 전송 시 바이트 수만큼 토큰 소비
- 부족하면 필요한 양이 찰 때까지 sleep
- 버스트 허용: 버킷 최대치(capacity)까지는 순간적으로 빠른 전송 허용

스레드 안전성:
- BandwidthLimiter 하나를 모든 워커 스레드가 공유하면
  전체 대역폭의 합이 제한값 이하로 수렴 (정확히 회사망 과부하 방지 목적)

시간대별 스케줄:
- 설정된 schedule을 매 5초마다 재평가
- 업무시간/점심/야간 등 시간대별로 다른 제한값 자동 전환
- 자정을 넘는 시간대(예: 22:00~07:00)도 정상 처리
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime
from typing import Optional

from gdrive_sync.config import BandwidthConfig, BandwidthSchedule


log = logging.getLogger(__name__)


# 1 MB = 1024 * 1024 bytes (IEC 이진 기준, 파일 크기 감각과 일치)
BYTES_PER_MB = 1024 * 1024

# 스케줄 재평가 간격 (초) — 매 전송마다 확인하면 오버헤드가 있으므로 캐싱
SCHEDULE_RECHECK_INTERVAL = 5.0


class TokenBucket:
    """스레드 안전 예약 기반 토큰 버킷.

    consume(n) 은 n bytes 분량의 대역폭 시작 시점을 예약하고
    그 시점까지 sleep 한다. Lock 안에서 예약 시점(_next_free)을 선착순으로
    전진시키므로 병렬 워커가 동시에 호출해도 각자 다른 시점을 예약 →
    모든 워커의 합산 속도가 rate 를 초과하지 않는다.

    capacity_bytes > 0 이면 해당 용량만큼 initial burst 허용
    (첫 호출들이 wait 없이 진행 가능한 누적 한도).
    rate=0 이면 무제한 (consume이 즉시 반환).
    """

    def __init__(self, rate_bytes_per_sec: float, capacity_bytes: Optional[float] = None):
        self.rate = max(0.0, rate_bytes_per_sec)
        if capacity_bytes is None:
            capacity_bytes = self.rate   # 기본 1초치
        # burst 허용 초수: _next_free 가 now 보다 이만큼 과거면 즉시 진행
        self._capacity_sec: float = (capacity_bytes / self.rate) if self.rate > 0 else 0.0
        # 시작 시 capacity_sec 만큼 과거로 세팅 → 초기 burst 허용
        self._next_free: float = time.monotonic() - self._capacity_sec
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        """n 바이트 전송 권한을 예약하고, 해당 시점까지 sleep."""
        if self.rate <= 0 or n <= 0:
            return

        with self._lock:
            now = time.monotonic()
            # burst 한도: _next_free 가 now 보다 최대 capacity_sec 이전까지만 허용
            start = max(self._next_free, now - self._capacity_sec)
            wait = start - now   # 음수면 즉시
            # 다음 워커를 위해 예약 시점 전진 (직렬화 핵심)
            self._next_free = start + n / self.rate

        if wait > 0:
            time.sleep(wait)

    def consume_with_progress(self, n: int, callback=None, stop_checker=None) -> None:
        """n 바이트를 1초치 단위로 분할 consume.

        단일 청크 모드(httplib2 308 fallback)에서 큰 파일 전체를 사전 차감할 때 사용.
        callback(consumed, total) 가 1초 간격으로 호출됨 → GUI에 카운트다운 표시 가능.
        stop_checker() 가 True 반환하면 즉시 종료 (남은 토큰은 차감 안 됨).
        """
        if self.rate <= 0 or n <= 0:
            if callback:
                try:
                    callback(n, n)
                except Exception:
                    pass
            return

        chunk = max(1024, int(self.rate))   # 1초치 (바이트 기준), 최소 1KB
        consumed = 0
        while consumed < n:
            if stop_checker and stop_checker():
                return
            portion = min(chunk, n - consumed)
            self.consume(portion)
            consumed += portion
            if callback:
                try:
                    callback(consumed, n)
                except Exception:
                    pass


class BandwidthLimiter:
    """업/다운 각각 별도 TokenBucket + 시간대별 자동 전환."""

    def __init__(
        self,
        upload_limit_mbps: float,
        download_limit_mbps: float,
        schedule: Optional[list[BandwidthSchedule]] = None,
    ):
        self.default_up = max(0.0, float(upload_limit_mbps))
        self.default_dn = max(0.0, float(download_limit_mbps))
        self.schedule = schedule or []

        self._upload_bucket: Optional[TokenBucket] = None
        self._download_bucket: Optional[TokenBucket] = None
        self._current_rule_name: str = ""
        self._last_check: float = 0.0
        self._reload_lock = threading.Lock()

        self._rebuild()

    # ──────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────

    def consume_upload(self, n: int) -> None:
        self._maybe_reload()
        if self._upload_bucket:
            self._upload_bucket.consume(n)

    def consume_download(self, n: int) -> None:
        self._maybe_reload()
        if self._download_bucket:
            self._download_bucket.consume(n)

    def consume_upload_with_progress(self, n: int, callback=None, stop_checker=None) -> None:
        """1초치 단위 분할 사전 consume. callback(consumed, total) 1초 간격."""
        self._maybe_reload()
        if self._upload_bucket:
            self._upload_bucket.consume_with_progress(n, callback, stop_checker)
        elif callback:
            try:
                callback(n, n)
            except Exception:
                pass

    def consume_download_with_progress(self, n: int, callback=None, stop_checker=None) -> None:
        self._maybe_reload()
        if self._download_bucket:
            self._download_bucket.consume_with_progress(n, callback, stop_checker)
        elif callback:
            try:
                callback(n, n)
            except Exception:
                pass

    def get_status(self) -> dict:
        """CLI status 명령용 상태 요약."""
        self._maybe_reload()
        up_mbps = self._bucket_rate_mbps(self._upload_bucket)
        dn_mbps = self._bucket_rate_mbps(self._download_bucket)
        return {
            "active_rule": self._current_rule_name or "default",
            "upload_mbps": up_mbps,
            "download_mbps": dn_mbps,
            "upload_unlimited": self._upload_bucket is None,
            "download_unlimited": self._download_bucket is None,
        }

    # ──────────────────────────────────────────────
    # 내부
    # ──────────────────────────────────────────────

    def _maybe_reload(self) -> None:
        """5초 이상 경과 시 스케줄 재평가."""
        now = time.monotonic()
        if now - self._last_check < SCHEDULE_RECHECK_INTERVAL:
            return
        with self._reload_lock:
            if now - self._last_check < SCHEDULE_RECHECK_INTERVAL:
                return  # double-check
            self._last_check = now
            self._rebuild()

    def _rebuild(self) -> None:
        """현재 시각 기준으로 활성 규칙을 골라 bucket 재생성."""
        up_mbps, dn_mbps, name = self._resolve_current_limits()
        self._current_rule_name = name
        self._upload_bucket = self._make_bucket(up_mbps)
        self._download_bucket = self._make_bucket(dn_mbps)
        log.debug(
            f"BandwidthLimiter: rule={name}, up={up_mbps} MB/s, dn={dn_mbps} MB/s"
        )

    def _resolve_current_limits(self) -> tuple[float, float, str]:
        """활성 스케줄 규칙 탐색 → (up_mbps, dn_mbps, name). 없으면 default."""
        now = datetime.now()
        for rule in self.schedule:
            if _rule_applies(rule, now):
                return (
                    rule.upload_limit_mbps,
                    rule.download_limit_mbps,
                    rule.name or "(unnamed)",
                )
        return self.default_up, self.default_dn, "default"

    @staticmethod
    def _make_bucket(mbps: float) -> Optional[TokenBucket]:
        if mbps <= 0:
            return None
        rate_bps = mbps * BYTES_PER_MB
        # burst = 0.5초치 — 초기 2초치는 병렬 워커가 동시에 소진해
        # N×rate 초과 burst를 유발했음. 예약 모델로 직렬화 후 burst도 축소.
        return TokenBucket(rate_bps, capacity_bytes=rate_bps * 0.5)

    @staticmethod
    def _bucket_rate_mbps(bucket: Optional[TokenBucket]) -> float:
        if bucket is None or bucket.rate <= 0:
            return 0.0
        return bucket.rate / BYTES_PER_MB


# ──────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────

_WEEKDAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


def _rule_applies(rule: BandwidthSchedule, now: datetime) -> bool:
    """주어진 시각에 이 규칙이 유효한지."""
    # 요일 필터
    if rule.weekdays:
        allowed = {_WEEKDAY_MAP.get(d.lower()) for d in rule.weekdays}
        allowed.discard(None)
        if now.weekday() not in allowed:
            return False

    # 시간 범위
    start = _parse_hhmm(rule.time_start)
    end = _parse_hhmm(rule.time_end)
    if start is None or end is None:
        return False

    current = now.time()
    if start <= end:
        return start <= current < end
    # 자정을 넘어가는 경우 (예: 22:00 ~ 07:00)
    return current >= start or current < end


def _parse_hhmm(s: str) -> Optional[dtime]:
    """'HH:MM' → datetime.time. 실패 시 None."""
    try:
        h, m = s.strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return None


def make_limiter(cfg: BandwidthConfig) -> Optional[BandwidthLimiter]:
    """설정에서 limiter 생성. enabled=False면 None 반환."""
    if not cfg.enabled:
        return None
    # 스케줄만 있고 기본값이 없어도 제한기 생성 (스케줄 시간대에만 제한)
    if cfg.upload_limit_mbps <= 0 and cfg.download_limit_mbps <= 0 and not cfg.schedule:
        return None
    return BandwidthLimiter(
        upload_limit_mbps=cfg.upload_limit_mbps,
        download_limit_mbps=cfg.download_limit_mbps,
        schedule=cfg.schedule,
    )
