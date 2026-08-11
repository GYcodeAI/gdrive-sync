"""충돌 해결 정책 테스트."""

from gdrive_sync.conflict import ConflictResolution, resolve


def test_newer_wins_local_newer():
    d = resolve("newer_wins", "a.txt", "2026-04-15T10:00:00Z", "2026-04-15T09:00:00Z")
    assert d.action == ConflictResolution.UPLOAD


def test_newer_wins_remote_newer():
    d = resolve("newer_wins", "a.txt", "2026-04-15T09:00:00Z", "2026-04-15T10:00:00Z")
    assert d.action == ConflictResolution.DOWNLOAD


def test_newer_wins_same_time_prefers_local():
    d = resolve("newer_wins", "a.txt", "2026-04-15T09:00:00Z", "2026-04-15T09:00:00Z")
    assert d.action == ConflictResolution.UPLOAD


def test_local_wins():
    d = resolve("local_wins", "a.txt", "2020-01-01T00:00:00Z", "2030-01-01T00:00:00Z")
    assert d.action == ConflictResolution.UPLOAD


def test_remote_wins():
    d = resolve("remote_wins", "a.txt", "2030-01-01T00:00:00Z", "2020-01-01T00:00:00Z")
    assert d.action == ConflictResolution.DOWNLOAD


def test_keep_both_generates_rename():
    d = resolve("keep_both", "folder/file.txt", "2026-04-15T10:00:00Z", "2026-04-15T10:00:00Z")
    assert d.action == ConflictResolution.KEEP_BOTH
    assert d.rename_to is not None
    assert "_conflict_" in d.rename_to
    assert d.rename_to.startswith("folder/")
    assert d.rename_to.endswith(".txt")


def test_keep_both_root_file():
    d = resolve("keep_both", "readme.md", "2026-04-15T10:00:00Z", "2026-04-15T10:00:00Z")
    assert d.action == ConflictResolution.KEEP_BOTH
    assert d.rename_to is not None
    assert "/" not in d.rename_to
    assert d.rename_to.endswith(".md")


def test_unknown_policy_defaults_to_newer():
    d = resolve("weird_policy", "a.txt", "2026-04-15T10:00:00Z", "2026-04-15T09:00:00Z")
    assert d.action == ConflictResolution.UPLOAD
