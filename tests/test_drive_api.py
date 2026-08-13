"""Drive API 래퍼의 가벼운 단위 테스트 (네트워크 없음)."""

from gdrive_sync.drive_api import (
    DriveClient, DriveFile, FOLDER_MIME, WORKSPACE_MIMES, _escape_q,
)


def test_drive_file_from_api_basic():
    df = DriveFile.from_api({
        "id": "abc",
        "name": "report.docx",
        "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "size": "12345",
        "modifiedTime": "2026-04-15T10:00:00.000Z",
        "md5Checksum": "deadbeef",
        "parents": ["parent-id"],
    })
    assert df.id == "abc"
    assert df.name == "report.docx"
    assert df.size == 12345
    assert df.md5 == "deadbeef"
    assert not df.is_folder
    assert not df.is_workspace_native


def test_drive_file_folder():
    df = DriveFile.from_api({
        "id": "folder1",
        "name": "업무",
        "mimeType": FOLDER_MIME,
    })
    assert df.is_folder
    assert df.size == 0


def test_drive_file_workspace_native():
    df = DriveFile.from_api({
        "id": "doc1",
        "name": "제안서",
        "mimeType": "application/vnd.google-apps.document",
    })
    assert df.is_workspace_native
    assert not df.is_folder


def test_drive_file_trashed_flag():
    df = DriveFile.from_api({
        "id": "x", "name": "x", "mimeType": "text/plain",
        "trashed": True,
    })
    assert df.trashed


def test_escape_q_quotes():
    assert _escape_q("it's a test") == "it\\'s a test"
    assert _escape_q("a\\b") == "a\\\\b"
    assert _escape_q("plain") == "plain"


def test_workspace_mimes_coverage():
    # 대표적인 Google 네이티브 mime이 포함되어 있는지
    assert "application/vnd.google-apps.document" in WORKSPACE_MIMES
    assert "application/vnd.google-apps.spreadsheet" in WORKSPACE_MIMES
    assert "application/vnd.google-apps.presentation" in WORKSPACE_MIMES


# ──────────────────────────────────────────────────────────
# _ensure_upload_metadata: 대용량 업로드 응답의 md5/modifiedTime 누락 보정
# (영구 재전송 버그 방지). __init__(네트워크) 우회 위해 __new__ 로 생성.
# ──────────────────────────────────────────────────────────

def _bare_client() -> DriveClient:
    return DriveClient.__new__(DriveClient)


def test_ensure_upload_metadata_complete_skips_refetch():
    """응답에 md5+modifiedTime 이 모두 있으면 재조회하지 않는다 (RTT 절약)."""
    client = _bare_client()
    calls = []
    client.get_file = lambda fid: calls.append(fid)  # 호출되면 안 됨

    full = DriveFile(
        id="f1", name="a.mp3", mime_type="audio/mpeg",
        size=110100480, modified_time="2026-06-15T00:00:00Z", md5="abc123",
    )
    out = client._ensure_upload_metadata(full)
    assert out is full
    assert calls == []


def test_ensure_upload_metadata_missing_fields_refetches():
    """md5/modifiedTime 이 빈 응답이면 get_file 로 권위 메타를 받아 교체한다."""
    client = _bare_client()
    authoritative = DriveFile(
        id="f1", name="a.mp3", mime_type="audio/mpeg",
        size=110100480, modified_time="2026-06-15T00:00:00Z", md5="realmd5",
    )
    seen = []
    client.get_file = lambda fid: seen.append(fid) or authoritative

    incomplete = DriveFile(
        id="f1", name="a.mp3", mime_type="audio/mpeg",
        size=110100480, modified_time="", md5="",
    )
    out = client._ensure_upload_metadata(incomplete)
    assert out is authoritative
    assert out.md5 == "realmd5"
    assert out.modified_time == "2026-06-15T00:00:00Z"
    assert seen == ["f1"]


def test_ensure_upload_metadata_refetch_failure_keeps_original():
    """재조회가 실패해도 예외를 삼키고 원본을 반환한다 (전송 자체는 성공이므로)."""
    client = _bare_client()

    def boom(fid):
        raise RuntimeError("network down")

    client.get_file = boom
    incomplete = DriveFile(id="f1", name="a.mp3", mime_type="audio/mpeg", md5="")
    out = client._ensure_upload_metadata(incomplete)
    assert out is incomplete


def test_ensure_upload_metadata_still_empty_after_refetch_keeps_original():
    """재조회해도 여전히 비어 있으면(처리 지연) 1회만 시도하고 원본 유지."""
    client = _bare_client()
    still_empty = DriveFile(id="f1", name="a.mp3", mime_type="audio/mpeg", md5="", modified_time="")
    client.get_file = lambda fid: still_empty

    incomplete = DriveFile(id="f1", name="a.mp3", mime_type="audio/mpeg", md5="", modified_time="")
    out = client._ensure_upload_metadata(incomplete)
    assert out is incomplete

# ──────────────────────────────────────────────────────────
# 403 rate limit 재시도 판정 (v2.4.2)
# ──────────────────────────────────────────────────────────

import json
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from gdrive_sync.drive_api import _is_rate_limit_403, _is_retryable


def _http_error(status: int, reason: str = "", use_details: bool = False):
    resp = MagicMock()
    resp.status = status
    resp.reason = "Forbidden"
    body = {"error": {"errors": [{"reason": reason}] if reason else []}}
    e = HttpError(resp, json.dumps(body).encode("utf-8"))
    if use_details:
        # 신버전 googleapiclient 의 error_details 경로
        e.error_details = [{"reason": reason}] if reason else []
    return e


class TestRateLimit403:
    def test_user_rate_limit_via_content(self):
        assert _is_rate_limit_403(_http_error(403, "userRateLimitExceeded"))

    def test_rate_limit_via_error_details(self):
        assert _is_rate_limit_403(
            _http_error(403, "rateLimitExceeded", use_details=True))

    def test_permission_403_not_rate_limit(self):
        assert not _is_rate_limit_403(_http_error(403, "insufficientPermissions"))

    def test_malformed_body_safe(self):
        resp = MagicMock()
        resp.status = 403
        e = HttpError(resp, b"not json at all")
        assert not _is_rate_limit_403(e)


class TestIsRetryable:
    def test_429_and_5xx_retryable(self):
        for status in (429, 500, 502, 503, 504):
            assert _is_retryable(_http_error(status), status)

    def test_403_rate_limit_retryable(self):
        e = _http_error(403, "userRateLimitExceeded")
        assert _is_retryable(e, 403)

    def test_403_permission_not_retryable(self):
        e = _http_error(403, "insufficientPermissions")
        assert not _is_retryable(e, 403)

    def test_404_not_retryable(self):
        assert not _is_retryable(_http_error(404), 404)
