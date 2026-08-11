"""ETA 계산기 — 30초 이동 평균 바이트 속도 기반.

대역폭 제한이 있어도 변동이 작아 단순 평균이 적절.
큰 파일 1개에 막혀있을 때도 청크 단위로 진행 추적되므로 정상 반영.

사용:
    eta = ETACalculator()
    eta.update(bytes_done=1_000_000)
    ...
    eta.update(bytes_done=2_500_000)
    rate = eta.rate_bps()                    # 50000.0 (B/s)
    sec  = eta.eta_seconds(remaining=10**8)   # 2000
    text = eta.format(remaining=10**8)        # "약 33분 (예상 완료 14:35)"
"""

from __future__ import annotations

import time
from collections import deque
from typing import Optional


class ETACalculator:
    """누적 바이트 진행을 받아 최근 N초 윈도우의 속도와 ETA를 계산."""

    def __init__(self, window_sec: float = 30.0, min_warmup_sec: float = 5.0):
        self.window_sec = window_sec
        self.min_warmup_sec = min_warmup_sec
        # (timestamp, cumulative_bytes) 샘플
        self._samples: deque[tuple[float, int]] = deque()
        self._start_time: Optional[float] = None

    def reset(self) -> None:
        """대역폭 스케줄 변경 등 환경 변화 시 호출 — 윈도우 비움."""
        self._samples.clear()
        self._start_time = None

    def update(self, bytes_done: int) -> None:
        """누적 전송 바이트 수 갱신."""
        now = time.time()
        if self._start_time is None:
            self._start_time = now
        self._samples.append((now, bytes_done))
        # 윈도우 벗어난 오래된 샘플 제거
        cutoff = now - self.window_sec
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def rate_bps(self) -> Optional[float]:
        """최근 윈도우 안의 평균 속도(bytes/sec). 측정 불가 시 None."""
        if len(self._samples) < 2:
            return None
        if self._start_time and (time.time() - self._start_time) < self.min_warmup_sec:
            return None
        first_t, first_b = self._samples[0]
        last_t, last_b = self._samples[-1]
        elapsed = last_t - first_t
        if elapsed <= 0:
            return None
        delta = last_b - first_b
        if delta <= 0:
            return 0.0
        return delta / elapsed

    def eta_seconds(self, remaining_bytes: int) -> Optional[float]:
        """남은 바이트 기준 ETA (초). 측정 불가 시 None."""
        if remaining_bytes <= 0:
            return 0.0
        rate = self.rate_bps()
        if rate is None or rate <= 0:
            return None
        return remaining_bytes / rate

    def format(self, remaining_bytes: int) -> str:
        """사람이 읽기 좋은 ETA 문자열.

        예: "측정 중...", "약 12분 (예상 완료 14:35)", "약 1시간 30분"
        """
        sec = self.eta_seconds(remaining_bytes)
        if sec is None:
            return "측정 중..."
        if sec < 1:
            return "곧 완료"

        completion = time.localtime(time.time() + sec)
        clock = time.strftime("%H:%M", completion)

        if sec < 60:
            return f"약 {int(sec)}초 (예상 완료 {clock})"
        if sec < 3600:
            return f"약 {int(sec / 60)}분 (예상 완료 {clock})"
        hours = int(sec / 3600)
        minutes = int((sec % 3600) / 60)
        if minutes:
            return f"약 {hours}시간 {minutes}분 (예상 완료 {clock})"
        return f"약 {hours}시간 (예상 완료 {clock})"

    def format_rate(self) -> str:
        """현재 속도 표시 — '2.3 MB/s' 형식."""
        rate = self.rate_bps()
        if rate is None:
            return "측정 중..."
        if rate < 1024:
            return f"{rate:.0f} B/s"
        if rate < 1024 * 1024:
            return f"{rate / 1024:.1f} KB/s"
        if rate < 1024 * 1024 * 1024:
            return f"{rate / 1024 / 1024:.1f} MB/s"
        return f"{rate / 1024 / 1024 / 1024:.2f} GB/s"
