"""전체 진행률 추적기 (B3+ — state.json 기반 추정 + 동적 보정).

설계:
- 시작 시: 모든 pair의 state.json을 읽어 분모 추정 (즉시 % 표시 가능)
- 미지수 pair (state 없음): 알려진 pair 평균값으로 임시 채움
- 각 pair 스캔 완료 시점에 분모 보정 (단조롭게 부드럽게)
- 진행 라벨: pair 모두 스캔 끝나기 전엔 [추정], 끝나면 [확정]

사용:
    pt = ProgressTracker(cfg)              # state 합산 → 즉시 분모
    pt.on_pair_scanned(pair, files=N, bytes=B)   # 보정 (단조 변화)
    pt.on_file_done(bytes_delta=4096)            # 진행 갱신
    pt.snapshot()                                # GUI 표시용 스냅샷
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from gdrive_sync.config import Config, SyncPair
from gdrive_sync.state import load_state


@dataclass
class ProgressSnapshot:
    """GUI에 전달할 한 시점의 진행 상태."""
    # 폴더 카운터
    pairs_total: int = 0
    pairs_done: int = 0           # 완료된 폴더 수
    current_pair_name: str = ""
    current_pair_progress: float = 0.0   # 0.0 ~ 1.0

    # 파일 카운터 (전체)
    files_done: int = 0
    files_total: int = 0          # 추정/확정 분모 (스무딩 적용된 표시값)
    files_total_raw: int = 0      # 보정 직후 실제값 (스무딩 전)

    # 바이트 (ETA용 + 표시용)
    bytes_done: int = 0
    bytes_total: int = 0

    # 라벨
    label: str = "[추정]"          # "[추정]" / "[추정 부분 확정]" / "[확정]"

    @property
    def overall_ratio(self) -> float:
        """전체 진행률 (0.0~1.0). 폴더 수 기반 + 현재 폴더 미세조정."""
        if self.pairs_total <= 0:
            return 0.0
        base = self.pairs_done / self.pairs_total
        addon = self.current_pair_progress / self.pairs_total if self.pairs_total else 0
        return min(1.0, base + addon)


class ProgressTracker:
    """동기화 전체 진행 추적."""

    SMOOTH_DURATION_SEC = 1.0   # 분모 변경 시 1초간 부드럽게 보간

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.pairs: list[SyncPair] = list(cfg.sync_pairs)
        self.pairs_total = len(self.pairs)

        # state 합산으로 즉시 분모 추정
        per_pair_files: dict[str, Optional[int]] = {}
        per_pair_bytes: dict[str, Optional[int]] = {}
        for pair in self.pairs:
            try:
                st = load_state(pair.local_path)
                if st.files:
                    per_pair_files[str(pair.local_path)] = len(st.files)
                    per_pair_bytes[str(pair.local_path)] = sum(
                        (f.local_size or 0) for f in st.files.values()
                    )
                else:
                    per_pair_files[str(pair.local_path)] = None
                    per_pair_bytes[str(pair.local_path)] = None
            except Exception:
                per_pair_files[str(pair.local_path)] = None
                per_pair_bytes[str(pair.local_path)] = None

        # 미지수 pair는 알려진 pair 평균값으로 채움
        known_files = [v for v in per_pair_files.values() if v is not None]
        known_bytes = [v for v in per_pair_bytes.values() if v is not None]
        avg_files = int(sum(known_files) / len(known_files)) if known_files else 0
        avg_bytes = int(sum(known_bytes) / len(known_bytes)) if known_bytes else 0

        for k, v in list(per_pair_files.items()):
            if v is None:
                per_pair_files[k] = avg_files
        for k, v in list(per_pair_bytes.items()):
            if v is None:
                per_pair_bytes[k] = avg_bytes

        self._pair_files: dict[str, int] = per_pair_files     # type: ignore
        self._pair_bytes: dict[str, int] = per_pair_bytes     # type: ignore
        self._scanned_pairs: set[str] = set()                  # 실제 스캔 완료된 pair 키

        # 분모 표시용 (스무딩 적용)
        self._displayed_files_total = sum(self._pair_files.values())
        self._target_files_total = self._displayed_files_total
        self._displayed_bytes_total = sum(self._pair_bytes.values())
        self._target_bytes_total = self._displayed_bytes_total
        self._smooth_start_time: Optional[float] = None
        self._smooth_from_files = self._displayed_files_total
        self._smooth_from_bytes = self._displayed_bytes_total

        # 진행 누적
        self.files_done: int = 0
        self.bytes_done: int = 0
        self.pairs_done: int = 0
        self.current_pair: Optional[SyncPair] = None
        self._current_pair_files_done = 0
        self._current_pair_files_total = 0

    # ──────────────────────────────────────────────
    # 이벤트 입력
    # ──────────────────────────────────────────────

    def on_pair_start(self, pair: SyncPair) -> None:
        self.current_pair = pair
        self._current_pair_files_done = 0
        # 추정값에서 가져옴
        self._current_pair_files_total = self._pair_files.get(str(pair.local_path), 0)

    def on_pair_scanned(self, pair: SyncPair, files: int, bytes_total: int) -> None:
        """스캔이 끝나서 실제 파일 수/바이트가 확정되면 분모 보정.

        스무딩: 즉시 점프하지 않고 1초 동안 부드럽게 이동.
        """
        key = str(pair.local_path)
        if key in self._scanned_pairs:
            return
        self._scanned_pairs.add(key)

        # 보정
        old_files = self._pair_files.get(key, 0)
        old_bytes = self._pair_bytes.get(key, 0)
        self._pair_files[key] = files
        self._pair_bytes[key] = bytes_total

        new_total_files = sum(self._pair_files.values())
        new_total_bytes = sum(self._pair_bytes.values())

        # 분모가 바뀌면 스무딩 시작
        if new_total_files != self._target_files_total or new_total_bytes != self._target_bytes_total:
            self._smooth_from_files = self._displayed_files_total
            self._smooth_from_bytes = self._displayed_bytes_total
            self._target_files_total = new_total_files
            self._target_bytes_total = new_total_bytes
            self._smooth_start_time = time.time()

        # 현재 pair 라면 현재 분모도 갱신
        if self.current_pair and str(self.current_pair.local_path) == key:
            self._current_pair_files_total = files

    def on_file_done(self, bytes_delta: int = 0) -> None:
        """한 파일 처리 완료 (전송/스킵/삭제 등 어떤 액션이든 카운트)."""
        self.files_done += 1
        self._current_pair_files_done += 1
        self.bytes_done += max(0, bytes_delta)

    def on_pair_done(self) -> None:
        self.pairs_done += 1
        self.current_pair = None
        self._current_pair_files_done = 0
        self._current_pair_files_total = 0

    # ──────────────────────────────────────────────
    # 출력
    # ──────────────────────────────────────────────

    def _smoothed_total(self) -> tuple[int, int]:
        """스무딩 적용된 분모 (files, bytes) 반환."""
        if self._smooth_start_time is None:
            return self._displayed_files_total, self._displayed_bytes_total

        elapsed = time.time() - self._smooth_start_time
        if elapsed >= self.SMOOTH_DURATION_SEC:
            self._displayed_files_total = self._target_files_total
            self._displayed_bytes_total = self._target_bytes_total
            self._smooth_start_time = None
            return self._displayed_files_total, self._displayed_bytes_total

        ratio = elapsed / self.SMOOTH_DURATION_SEC
        f = int(self._smooth_from_files + (self._target_files_total - self._smooth_from_files) * ratio)
        b = int(self._smooth_from_bytes + (self._target_bytes_total - self._smooth_from_bytes) * ratio)
        self._displayed_files_total = f
        self._displayed_bytes_total = b
        return f, b

    def _label(self) -> str:
        scanned = len(self._scanned_pairs)
        if scanned == 0:
            return "[추정]"
        if scanned < self.pairs_total:
            return f"[부분 확정 {scanned}/{self.pairs_total}]"
        return "[확정]"

    def snapshot(self) -> ProgressSnapshot:
        files_total, bytes_total = self._smoothed_total()

        if self._current_pair_files_total > 0:
            current_progress = self._current_pair_files_done / self._current_pair_files_total
            current_progress = min(1.0, current_progress)
        else:
            current_progress = 0.0

        return ProgressSnapshot(
            pairs_total=self.pairs_total,
            pairs_done=self.pairs_done,
            current_pair_name=(self.current_pair.local_path.name if self.current_pair else ""),
            current_pair_progress=current_progress,
            files_done=self.files_done,
            files_total=max(self.files_done, files_total),  # 분모가 카운트보다 작아지지 않게
            files_total_raw=self._target_files_total,
            bytes_done=self.bytes_done,
            bytes_total=max(self.bytes_done, bytes_total),
            label=self._label(),
        )
