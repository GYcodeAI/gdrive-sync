"""update_check 모듈 테스트 — 버전 파싱/비교, 태그 선별, 스로틀 캐시."""

import json
import time

import pytest

from gdrive_sync import update_check as uc
from gdrive_sync.update_check import (
    UpdateInfo, _latest_from_tags, check_for_update, parse_version,
)


# ──────────────────────────────────────────────────────────
# parse_version
# ──────────────────────────────────────────────────────────

class TestParseVersion:
    def test_v_prefix(self):
        assert parse_version("v1.2.3") == (1, 2, 3)

    def test_no_prefix(self):
        assert parse_version("0.1.0") == (0, 1, 0)

    def test_two_part(self):
        assert parse_version("v2.4") == (2, 4)

    def test_invalid_returns_empty(self):
        assert parse_version("release-candidate") == ()
        assert parse_version("v1.2.3-beta") == ()
        assert parse_version("") == ()

    def test_comparison(self):
        assert parse_version("v0.10.0") > parse_version("v0.9.9")
        assert parse_version("v1.0") > parse_version("v0.99.99")


# ──────────────────────────────────────────────────────────
# _latest_from_tags
# ──────────────────────────────────────────────────────────

class TestLatestFromTags:
    def test_picks_highest(self):
        assert _latest_from_tags(["v0.1.0", "v0.3.0", "v0.2.0"]) == "v0.3.0"

    def test_ignores_non_version_tags(self):
        assert _latest_from_tags(["backup-2024", "v0.2.0", "wip"]) == "v0.2.0"

    def test_all_invalid(self):
        assert _latest_from_tags(["foo", "bar"]) is None

    def test_empty(self):
        assert _latest_from_tags([]) is None


# ──────────────────────────────────────────────────────────
# UpdateInfo
# ──────────────────────────────────────────────────────────

class TestUpdateInfo:
    def test_available_when_newer(self):
        assert UpdateInfo(current="0.1.0", latest="v0.2.0").available

    def test_not_available_when_same(self):
        assert not UpdateInfo(current="0.2.0", latest="v0.2.0").available

    def test_not_available_when_older_remote(self):
        assert not UpdateInfo(current="0.3.0", latest="v0.2.0").available

    def test_upgrade_command_mentions_repo(self):
        assert "github.com" in UpdateInfo(current="0.1.0", latest="v0.2.0").upgrade_command()


# ──────────────────────────────────────────────────────────
# check_for_update — 스로틀 캐시 (네트워크는 monkeypatch)
# ──────────────────────────────────────────────────────────

@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    p = tmp_path / "update_check.json"
    monkeypatch.setattr(uc, "CACHE_PATH", p)
    return p


class TestCheckForUpdate:
    def test_network_success_writes_cache(self, cache_path, monkeypatch):
        monkeypatch.setattr(uc, "fetch_latest_version", lambda timeout: "v9.9.9")
        info = check_for_update()
        assert info is not None and info.available
        assert json.loads(cache_path.read_text())["latest"] == "v9.9.9"

    def test_throttled_uses_cache_without_network(self, cache_path, monkeypatch):
        cache_path.write_text(json.dumps({"checked_at": time.time(), "latest": "v9.9.9"}))

        def boom(timeout):
            raise AssertionError("스로틀 중에는 네트워크 조회하면 안 됨")
        monkeypatch.setattr(uc, "fetch_latest_version", boom)

        info = check_for_update()
        assert info is not None and info.latest == "v9.9.9"

    def test_force_bypasses_throttle(self, cache_path, monkeypatch):
        cache_path.write_text(json.dumps({"checked_at": time.time(), "latest": "v0.0.1"}))
        monkeypatch.setattr(uc, "fetch_latest_version", lambda timeout: "v9.9.9")
        info = check_for_update(force=True)
        assert info.latest == "v9.9.9"

    def test_stale_cache_triggers_refetch(self, cache_path, monkeypatch):
        cache_path.write_text(json.dumps(
            {"checked_at": time.time() - uc.CHECK_INTERVAL_SEC - 10, "latest": "v0.0.1"}))
        monkeypatch.setattr(uc, "fetch_latest_version", lambda timeout: "v9.9.9")
        info = check_for_update()
        assert info.latest == "v9.9.9"

    def test_fetch_failure_keeps_previous_latest(self, cache_path, monkeypatch):
        cache_path.write_text(json.dumps(
            {"checked_at": time.time() - uc.CHECK_INTERVAL_SEC - 10, "latest": "v9.9.9"}))
        monkeypatch.setattr(uc, "fetch_latest_version", lambda timeout: None)
        info = check_for_update()
        assert info is not None and info.latest == "v9.9.9"
        # 실패해도 checked_at 은 갱신돼 재시도 폭주 방지
        assert time.time() - json.loads(cache_path.read_text())["checked_at"] < 60

    def test_no_cache_and_fetch_failure_returns_none(self, cache_path, monkeypatch):
        monkeypatch.setattr(uc, "fetch_latest_version", lambda timeout: None)
        assert check_for_update() is None

    def test_never_raises_on_corrupt_cache(self, cache_path, monkeypatch):
        cache_path.write_text("{{{ not json")
        monkeypatch.setattr(uc, "fetch_latest_version", lambda timeout: "v9.9.9")
        info = check_for_update()
        assert info.latest == "v9.9.9"
