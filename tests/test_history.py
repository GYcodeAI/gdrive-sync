"""history.py 단위 테스트."""

from datetime import datetime, timezone

from gdrive_sync.history import (
    SyncRecord,
    append_record,
    format_timestamp_local,
    get_stats,
    load_history,
)


# ──────────────────────────────────────────────────────────
# format_timestamp_local
# ──────────────────────────────────────────────────────────

def test_utc_timestamp_converted_to_local():
    """UTC Z 타임스탬프가 로컬 시각으로 변환되어야 한다."""
    # 2026-05-12T01:08:39Z (UTC) → 로컬 변환 후 포맷 검증
    ts_utc = "2026-05-12T01:08:39Z"
    result = format_timestamp_local(ts_utc)

    # 결과가 "YYYY-MM-DD HH:MM:SS" 형식인지 확인
    assert len(result) == 19
    dt_check = datetime.strptime(result, "%Y-%m-%d %H:%M:%S")

    # UTC → 로컬 변환한 값과 일치하는지 확인
    expected = (
        datetime.fromisoformat("2026-05-12T01:08:39+00:00")
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )
    assert result == expected


def test_utc_differs_from_raw_string_in_nonzero_offset():
    """UTC+0이 아닌 로컬 환경에서 UTC 문자열과 변환 결과가 다르다."""
    ts_utc = "2026-05-12T01:08:39Z"
    raw = ts_utc[:19].replace("T", " ")  # 변환 전 (잘못된 표시)
    converted = format_timestamp_local(ts_utc)

    local_offset = datetime.now(timezone.utc).astimezone().utcoffset().total_seconds()
    if local_offset != 0:
        assert converted != raw, "UTC+0이 아닌 환경에서는 변환 전후가 달라야 함"


def test_invalid_timestamp_falls_back_gracefully():
    """파싱 불가능한 타임스탬프는 원본을 그대로 반환한다 (이전 데이터 호환)."""
    bad = "not-a-timestamp"
    result = format_timestamp_local(bad)
    assert "not" in result  # 원본 내용 유지


def test_already_local_format_does_not_crash():
    """'T'와 'Z' 없는 구 형식도 예외 없이 처리된다."""
    old_fmt = "2026-05-11 07:52:47"
    result = format_timestamp_local(old_fmt)
    assert isinstance(result, str)


# ──────────────────────────────────────────────────────────
# SyncRecord 바이트 집계
# ──────────────────────────────────────────────────────────

def test_get_stats_sums_bytes():
    """get_stats가 uploaded_bytes / downloaded_bytes를 정확히 합산한다."""
    records = [
        SyncRecord(
            timestamp="2026-05-12T01:00:00Z",
            uploaded=3, uploaded_bytes=1_500_000,
            downloaded=1, downloaded_bytes=500_000,
        ),
        SyncRecord(
            timestamp="2026-05-12T02:00:00Z",
            uploaded=5, uploaded_bytes=2_000_000,
            downloaded=2, downloaded_bytes=800_000,
        ),
    ]
    stats = get_stats(records)
    assert stats["total_uploaded_bytes"] == 3_500_000
    assert stats["total_downloaded_bytes"] == 1_300_000


def test_get_stats_bytes_zero_when_not_set():
    """bytes 필드 미설정(구 기록) 시 0으로 집계된다."""
    records = [
        SyncRecord(timestamp="2026-05-12T01:00:00Z", uploaded=10),
    ]
    stats = get_stats(records)
    assert stats["total_uploaded_bytes"] == 0
    assert stats["total_downloaded_bytes"] == 0


# ──────────────────────────────────────────────────────────
# append_record / load_history
# ──────────────────────────────────────────────────────────

def test_append_and_load_roundtrip(tmp_path):
    """기록 저장 후 로드하면 동일한 값이 반환된다."""
    p = tmp_path / "history.json"
    record = SyncRecord(
        timestamp="2026-05-12T01:08:39Z",
        uploaded=7,
        uploaded_bytes=1_234_567,
        downloaded=2,
        downloaded_bytes=89_000,
        errors=0,
        elapsed_sec=42.5,
    )
    append_record(record, path=p)
    loaded = load_history(path=p)

    assert len(loaded) == 1
    assert loaded[0].uploaded == 7
    assert loaded[0].uploaded_bytes == 1_234_567
    assert loaded[0].downloaded_bytes == 89_000
    assert loaded[0].timestamp == "2026-05-12T01:08:39Z"
