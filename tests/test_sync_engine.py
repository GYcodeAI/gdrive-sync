"""동기화 엔진 의사결정 로직 테스트 (Mock Drive 사용)."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gdrive_sync.config import Config, NetworkConfig, SyncPair
from gdrive_sync.drive_api import DriveFile, FOLDER_MIME
from gdrive_sync.local_scanner import LocalFile
from gdrive_sync.state import FileState, SyncState
from gdrive_sync.sync_engine import ActionType, SyncEngine


def _local(rel: str, size: int = 100, mtime: str = "2026-04-15T10:00:00Z",
           md5: str = "abc") -> LocalFile:
    lf = LocalFile(
        rel_path=rel,
        abs_path=Path("/tmp") / rel,
        size=size,
        mtime_iso=mtime,
        is_folder=False,
    )
    lf._md5 = md5
    return lf


def _remote(rel: str, size: int = 100, mtime: str = "2026-04-15T10:00:00Z",
            md5: str = "abc") -> DriveFile:
    return DriveFile(
        id="id-" + rel.replace("/", "-"),
        name=rel.split("/")[-1],
        mime_type="text/plain",
        size=size,
        modified_time=mtime,
        md5=md5,
    )


def _make_engine(cfg: Config = None) -> SyncEngine:
    cfg = cfg or Config(conflict_policy="newer_wins")
    return SyncEngine(cfg=cfg, drive=MagicMock(), dry_run=True)


def test_upload_new_when_only_local():
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt")},
        remote={},
        state=SyncState(),
    )
    assert len(actions) == 1
    assert actions[0].type == ActionType.UPLOAD_NEW
    assert actions[0].rel_path == "a.txt"


def test_download_new_when_only_remote():
    engine = _make_engine()
    actions = engine._decide_actions(
        local={},
        remote={"a.txt": _remote("a.txt")},
        state=SyncState(),
    )
    assert len(actions) == 1
    assert actions[0].type == ActionType.DOWNLOAD_NEW


def test_same_content_skip():
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=5, md5="x")},
        remote={"a.txt": _remote("a.txt", size=5, md5="x")},
        state=SyncState(),
    )
    assert actions[0].type == ActionType.SKIP_SAME


# ──────────────────────────────────────────────────────────
# C-2: 큰 파일 size+mtime 매칭 (MD5 생략)
# ──────────────────────────────────────────────────────────

def test_large_file_size_mtime_match_skips_md5():
    """100MB 이상 + size 동일 + mtime ±2초 이내 → SKIP_SAME (MD5 안 봄)."""
    engine = _make_engine()
    big = 200 * 1024 * 1024
    # MD5는 일부러 다르게 — 호출 안 됨을 검증
    lf = _local("big.bin", size=big, mtime="2026-04-15T10:00:00Z", md5="LOCAL")
    rf = _remote("big.bin", size=big, mtime="2026-04-15T10:00:01Z", md5="REMOTE")
    actions = engine._decide_actions(
        local={"big.bin": lf}, remote={"big.bin": rf}, state=SyncState(),
    )
    assert actions[0].type == ActionType.SKIP_SAME


def test_large_file_mtime_far_triggers_conflict():
    """100MB 이상 + size 동일 + mtime 크게 다름 → 충돌 처리."""
    engine = _make_engine()
    big = 200 * 1024 * 1024
    lf = _local("big.bin", size=big, mtime="2026-04-15T10:00:00Z", md5="A")
    rf = _remote("big.bin", size=big, mtime="2026-04-15T11:00:00Z", md5="B")
    actions = engine._decide_actions(
        local={"big.bin": lf}, remote={"big.bin": rf}, state=SyncState(),
    )
    # newer_wins → remote 이 더 최신이라 충돌 다운로드
    assert actions[0].type in (
        ActionType.CONFLICT_DOWNLOAD, ActionType.DOWNLOAD_NEW,
        ActionType.DOWNLOAD_UPDATE,
    )


def test_small_file_still_uses_md5():
    """임계치 미만 파일은 MD5 비교 그대로 — md5 다르면 SKIP_SAME 아님."""
    engine = _make_engine()
    small = 1024
    lf = _local("small.txt", size=small, mtime="2026-04-15T10:00:00Z", md5="A")
    rf = _remote("small.txt", size=small, mtime="2026-04-15T10:00:00Z", md5="B")
    actions = engine._decide_actions(
        local={"small.txt": lf}, remote={"small.txt": rf}, state=SyncState(),
    )
    assert actions[0].type != ActionType.SKIP_SAME


def test_threshold_zero_disables_md5_skip():
    """large_file_md5_skip_mb=0 이면 큰 파일도 MD5 비교 (기존 동작)."""
    cfg = Config(conflict_policy="newer_wins")
    cfg.performance.large_file_md5_skip_mb = 0
    engine = _make_engine(cfg)
    big = 500 * 1024 * 1024
    lf = _local("big.bin", size=big, mtime="2026-04-15T10:00:00Z", md5="X")
    rf = _remote("big.bin", size=big, mtime="2026-04-15T10:00:00Z", md5="Y")
    actions = engine._decide_actions(
        local={"big.bin": lf}, remote={"big.bin": rf}, state=SyncState(),
    )
    assert actions[0].type != ActionType.SKIP_SAME


def test_local_modified_uploads():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="old",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="old",
        remote_id="id-a",
    )
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=20, mtime="2026-04-15T10:00:00Z", md5="new")},
        remote={"a.txt": _remote("a.txt", size=10, mtime="2020-01-01T00:00:00Z", md5="old")},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.UPLOAD_UPDATE


def test_remote_modified_downloads():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="old",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="old",
        remote_id="id-a",
    )
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=10, mtime="2020-01-01T00:00:00Z", md5="old")},
        remote={"a.txt": _remote("a.txt", size=15, mtime="2026-04-15T10:00:00Z", md5="new")},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.DOWNLOAD_UPDATE


def test_both_modified_triggers_conflict_newer_wins():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="old",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="old",
        remote_id="id-a",
    )
    engine = _make_engine(Config(conflict_policy="newer_wins"))
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=20, mtime="2026-04-15T10:00:00Z", md5="local")},
        remote={"a.txt": _remote("a.txt", size=15, mtime="2026-04-15T09:00:00Z", md5="remote")},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.CONFLICT_UPLOAD


def test_both_modified_remote_newer():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="old",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="old",
        remote_id="id-a",
    )
    engine = _make_engine(Config(conflict_policy="newer_wins"))
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=20, mtime="2026-04-15T09:00:00Z", md5="local")},
        remote={"a.txt": _remote("a.txt", size=15, mtime="2026-04-15T10:00:00Z", md5="remote")},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.CONFLICT_DOWNLOAD


def test_deleted_local_triggers_delete_remote():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="abc",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="abc",
        remote_id="id-a",
    )
    engine = _make_engine()
    actions = engine._decide_actions(
        local={},
        remote={"a.txt": _remote("a.txt", size=10, mtime="2020-01-01T00:00:00Z", md5="abc")},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.DELETE_REMOTE


def test_deleted_remote_triggers_delete_local():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="abc",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="abc",
        remote_id="id-a",
    )
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=10, mtime="2020-01-01T00:00:00Z", md5="abc")},
        remote={},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.DELETE_LOCAL


def test_both_deleted_removes_state():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z", local_md5="abc",
        remote_size=10, remote_mtime="2020-01-01T00:00:00Z", remote_md5="abc",
        remote_id="id-a",
    )
    engine = _make_engine()
    actions = engine._decide_actions(
        local={}, remote={},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.REMOVE_STATE


def test_remove_state_counted_in_summary(caplog):
    """REMOVE_STATE 처리 시 summary.removed_state 증가 + 샘플 기록 + 로그 출력.

    이전엔 조용히 state 만 정리해 운영자가 'ghost-cleanup' 패턴을 놓쳤음.
    """
    import logging
    from gdrive_sync.config import Config, SyncPair
    from gdrive_sync.sync_engine import Action, ActionType, SyncSummary

    cfg = Config(conflict_policy="newer_wins")
    engine = SyncEngine(cfg=cfg, drive=MagicMock(), dry_run=False)
    pair = SyncPair(local_path=Path("/tmp"), remote_path="r")
    state = SyncState(files={"a.txt": FileState(remote_id="id1")})
    summary = SyncSummary()

    action = Action(ActionType.REMOVE_STATE, "a.txt", None, None,
                    state.files["a.txt"])

    with caplog.at_level(logging.INFO, logger="gdrive_sync.sync_engine"):
        engine._execute_sequential_one(pair, action, "root-id", state, summary)

    assert summary.removed_state == 1
    assert summary.removed_state_samples == ["a.txt"]
    assert "a.txt" not in state.files
    assert any("state 정리" in r.message for r in caplog.records)


def test_delete_local_logs_success(caplog, tmp_path):
    """_do_delete_local 성공 시 INFO 로그 — 이전엔 침묵."""
    import logging
    from gdrive_sync.config import Config, SyncPair
    from gdrive_sync.sync_engine import Action, ActionType, SyncSummary

    # 실제 파일 만들어두기 (휴지통 이동 동작)
    target = tmp_path / "doomed.txt"
    target.write_text("bye")
    lf = _local("doomed.txt")
    lf.abs_path = target

    cfg = Config(conflict_policy="newer_wins", delete_policy="trash")
    cfg.trash.central = True
    cfg.trash.central_path = str(tmp_path / "_trash")
    engine = SyncEngine(cfg=cfg, drive=MagicMock(), dry_run=False)
    pair = SyncPair(local_path=tmp_path, remote_path="r")
    state = SyncState(files={"doomed.txt": FileState(remote_id="x")})
    summary = SyncSummary()

    action = Action(ActionType.DELETE_LOCAL, "doomed.txt", lf, None,
                    state.files["doomed.txt"])

    with caplog.at_level(logging.INFO, logger="gdrive_sync.sync_engine"):
        engine._execute_sequential_one(pair, action, "root", state, summary)

    assert summary.deleted_local == 1
    assert any("휴지통" in r.message for r in caplog.records)


def _delete_engine(tmp_path):
    """빈 폴더 정리 테스트용 엔진/페어 헬퍼 (permanent 삭제로 휴지통 노이즈 제거)."""
    from gdrive_sync.sync_engine import Action, ActionType, SyncSummary
    cfg = Config(conflict_policy="newer_wins", delete_policy="permanent")
    engine = SyncEngine(cfg=cfg, drive=MagicMock(), dry_run=False)
    pair = SyncPair(local_path=tmp_path, remote_path="r")
    return engine, pair


def _delete_one(engine, pair, rel):
    """rel 경로 파일 1개를 DELETE_LOCAL 실행."""
    from gdrive_sync.sync_engine import Action, ActionType, SyncSummary
    target = pair.local_path / rel
    lf = _local(rel)
    lf.abs_path = target
    state = SyncState(files={rel: FileState(remote_id="x")})
    summary = SyncSummary()
    action = Action(ActionType.DELETE_LOCAL, rel, lf, None, state.files[rel])
    engine._execute_sequential_one(pair, action, "root", state, summary)
    return summary


def test_prune_removes_emptied_parent_dirs(tmp_path):
    """파일 삭제로 비게 된 상위 폴더가 페어 루트까지 자동 제거됨."""
    engine, pair = _delete_engine(tmp_path)
    sub = tmp_path / "공모전" / "하위"
    sub.mkdir(parents=True)
    (sub / "doc.txt").write_text("x")

    summary = _delete_one(engine, pair, "공모전/하위/doc.txt")

    assert summary.deleted_local == 1
    assert summary.pruned_dirs == 2  # 하위 + 공모전
    assert not (tmp_path / "공모전").exists()
    assert tmp_path.exists()  # 페어 루트는 절대 보존


def test_prune_stops_at_nonempty_parent(tmp_path):
    """형제 파일이 남은 폴더는 제거되지 않고, 그 위로도 안 올라감."""
    engine, pair = _delete_engine(tmp_path)
    folder = tmp_path / "교육연구부"
    folder.mkdir()
    (folder / "doomed.txt").write_text("x")
    (folder / "keep.txt").write_text("keep")  # 형제 — 폴더 유지돼야 함

    summary = _delete_one(engine, pair, "교육연구부/doomed.txt")

    assert summary.pruned_dirs == 0
    assert folder.exists()
    assert (folder / "keep.txt").exists()


def test_prune_treats_os_junk_as_empty(tmp_path):
    """`.DS_Store` 등 OS 메타데이터만 남은 폴더도 비어있는 것으로 보고 제거."""
    engine, pair = _delete_engine(tmp_path)
    folder = tmp_path / "신임 이사 위촉"
    folder.mkdir()
    (folder / "doomed.txt").write_text("x")
    (folder / ".DS_Store").write_text("junk")

    summary = _delete_one(engine, pair, "신임 이사 위촉/doomed.txt")

    assert summary.pruned_dirs == 1
    assert not folder.exists()  # junk 까지 함께 정리


def test_prune_never_removes_pair_root(tmp_path):
    """페어 루트 바로 아래 파일을 지워도 루트는 절대 제거 안 함."""
    engine, pair = _delete_engine(tmp_path)
    (tmp_path / "top.txt").write_text("x")

    summary = _delete_one(engine, pair, "top.txt")

    assert summary.deleted_local == 1
    assert summary.pruned_dirs == 0
    assert tmp_path.exists()


def test_sweep_dryrun_lists_without_removing(tmp_path):
    """--local dry-run: 중첩 빈 폴더를 계단식으로 모두 찾되 실제로는 안 지움."""
    from gdrive_sync.sync_engine import sweep_empty_dirs
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)

    removed = sweep_empty_dirs(tmp_path, apply=False)

    rels = {str(r) for r in removed}
    assert rels == {"a", str(Path("a") / "b"), str(Path("a") / "b" / "c")}
    assert (tmp_path / "a" / "b" / "c").exists()  # dry-run — 보존


def test_sweep_apply_removes_cascade(tmp_path):
    """--local --apply: 자식부터 제거해 부모까지 비면 함께 제거. 형제 파일은 보존."""
    from gdrive_sync.sync_engine import sweep_empty_dirs
    (tmp_path / "empty" / "deep").mkdir(parents=True)
    keep = tmp_path / "keep"
    keep.mkdir()
    (keep / "file.txt").write_text("x")

    removed = sweep_empty_dirs(tmp_path, apply=True)

    assert {str(r) for r in removed} == {"empty", str(Path("empty") / "deep")}
    assert not (tmp_path / "empty").exists()
    assert keep.exists() and (keep / "file.txt").exists()
    assert tmp_path.exists()  # 루트 보존


def test_sweep_treats_junk_as_empty(tmp_path):
    """OS junk(.DS_Store)만 든 폴더도 비운 것으로 보고 junk 째 제거."""
    from gdrive_sync.sync_engine import sweep_empty_dirs
    junkdir = tmp_path / "공모전"
    junkdir.mkdir()
    (junkdir / ".DS_Store").write_text("junk")

    removed = sweep_empty_dirs(tmp_path, apply=True)

    assert {str(r) for r in removed} == {"공모전"}
    assert not junkdir.exists()


def test_sweep_never_touches_root(tmp_path):
    """루트가 비어 있어도 루트 자체는 절대 제거 대상이 아님."""
    from gdrive_sync.sync_engine import sweep_empty_dirs
    removed = sweep_empty_dirs(tmp_path, apply=True)
    assert removed == []
    assert tmp_path.exists()


def test_force_upload_overrides():
    prior = FileState(
        local_size=10, local_mtime="2020-01-01T00:00:00Z",
        remote_size=15, remote_mtime="2026-04-15T10:00:00Z", remote_id="id",
    )
    engine = SyncEngine(
        cfg=Config(conflict_policy="newer_wins"),
        drive=MagicMock(), dry_run=True,
        force_mode="upload",
    )
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt", size=10, md5="old")},
        remote={"a.txt": _remote("a.txt", size=15, md5="new")},
        state=SyncState(files={"a.txt": prior}),
    )
    assert actions[0].type == ActionType.UPLOAD_UPDATE


def test_force_download_skips_unchanged_files():
    """회귀: 다운로드만 모드에서도 변경 없는 파일은 SKIP_SAME 이어야 함.

    과거 버그: force_mode=download가 비교 없이 무조건 DOWNLOAD_UPDATE로 돌려서
    수만 개 파일 / 수백 GB 데이터를 매 실행마다 재다운로드. 회사망/시간 낭비.
    """
    prior = FileState(
        local_size=100, local_mtime="2026-04-23T10:00:00Z",
        local_md5="abc",
        remote_id="id-foo",
        remote_size=100, remote_mtime="2026-04-23T10:00:00Z",
        remote_md5="abc",
    )
    engine = SyncEngine(
        cfg=Config(), drive=MagicMock(), dry_run=True,
        force_mode="download",
    )
    actions = engine._decide_actions(
        local={"foo.txt": _local("foo.txt", size=100,
                                  mtime="2026-04-23T10:00:00Z", md5="abc")},
        remote={"foo.txt": _remote("foo.txt", size=100,
                                    mtime="2026-04-23T10:00:00Z", md5="abc")},
        state=SyncState(files={"foo.txt": prior}),
    )
    assert actions[0].type == ActionType.SKIP_SAME, (
        f"변경 없는 파일은 SKIP_SAME 이어야 하는데 {actions[0].type}"
    )


def test_force_upload_skips_unchanged_files():
    """회귀: 업로드만 모드에서도 변경 없는 파일은 SKIP_SAME 이어야 함."""
    prior = FileState(
        local_size=100, local_mtime="2026-04-23T10:00:00Z",
        local_md5="abc",
        remote_id="id-foo",
        remote_size=100, remote_mtime="2026-04-23T10:00:00Z",
        remote_md5="abc",
    )
    engine = SyncEngine(
        cfg=Config(), drive=MagicMock(), dry_run=True,
        force_mode="upload",
    )
    actions = engine._decide_actions(
        local={"foo.txt": _local("foo.txt", size=100,
                                  mtime="2026-04-23T10:00:00Z", md5="abc")},
        remote={"foo.txt": _remote("foo.txt", size=100,
                                    mtime="2026-04-23T10:00:00Z", md5="abc")},
        state=SyncState(files={"foo.txt": prior}),
    )
    assert actions[0].type == ActionType.SKIP_SAME


def test_force_download_still_downloads_changed_remote():
    """미러 모드에서 리모트가 변경됐으면 정상 다운로드되어야 함."""
    prior = FileState(
        local_size=100, local_mtime="2026-04-23T10:00:00Z", local_md5="old",
        remote_id="id-foo",
        remote_size=100, remote_mtime="2026-04-23T10:00:00Z", remote_md5="old",
    )
    engine = SyncEngine(
        cfg=Config(), drive=MagicMock(), dry_run=True,
        force_mode="download",
    )
    actions = engine._decide_actions(
        local={"foo.txt": _local("foo.txt", size=100,
                                  mtime="2026-04-23T10:00:00Z", md5="old")},
        # 리모트 mtime/md5/size가 변경됨
        remote={"foo.txt": _remote("foo.txt", size=200,
                                    mtime="2026-04-29T15:00:00Z", md5="new")},
        state=SyncState(files={"foo.txt": prior}),
    )
    assert actions[0].type == ActionType.DOWNLOAD_UPDATE


def test_force_upload_mirrors_deleting_remote_only():
    """force_mode=upload: Drive에만 있는 파일은 DELETE_REMOTE로 정리되어야 함.

    의도: 업로드만 모드 = 로컬 → Drive 완전 미러.
    과거 버그: 반대편만 있는 파일이 9-시나리오로 빠져 DOWNLOAD_NEW가 되거나
               DELETE_REMOTE가 조건부로만 됐음. 이제 항상 DELETE_REMOTE.
    """
    engine = SyncEngine(
        cfg=Config(), drive=MagicMock(), dry_run=True,
        force_mode="upload",
    )
    # 내용이 다른 두 파일 — 같으면 rename 감지가 (올바르게) MOVE 로 바꿔버림
    actions = engine._decide_actions(
        local={"local_only.txt": _local("local_only.txt", size=10, md5="L")},
        remote={"remote_only.txt": _remote("remote_only.txt", size=20, md5="R")},
        state=SyncState(),
    )
    by_rel = {a.rel_path: a for a in actions}
    assert by_rel["local_only.txt"].type == ActionType.UPLOAD_NEW
    assert by_rel["remote_only.txt"].type == ActionType.DELETE_REMOTE


def test_force_download_mirrors_deleting_local_only():
    """force_mode=download: 로컬에만 있는 파일은 DELETE_LOCAL로 정리되어야 함.

    의도: 다운로드만 모드 = Drive → 로컬 완전 미러.
    """
    engine = SyncEngine(
        cfg=Config(), drive=MagicMock(), dry_run=True,
        force_mode="download",
    )
    # 내용이 다른 두 파일 — 같으면 rename 감지가 (올바르게) MOVE 로 바꿔버림
    actions = engine._decide_actions(
        local={"local_only.txt": _local("local_only.txt", size=10, md5="L")},
        remote={"remote_only.txt": _remote("remote_only.txt", size=20, md5="R")},
        state=SyncState(),
    )
    by_rel = {a.rel_path: a for a in actions}
    assert by_rel["remote_only.txt"].type == ActionType.DOWNLOAD_NEW
    assert by_rel["local_only.txt"].type == ActionType.DELETE_LOCAL


def test_zero_byte_file_stays_synced():
    """회귀 테스트: 0바이트 파일이 무한 재업로드되지 않아야 한다.

    과거 버그: `prior.local_size or -1`이 0을 -1로 바꿔서 항상
    '로컬 변경됨'으로 오판 → 매 실행마다 UPLOAD_UPDATE.
    py.typed 같은 빈 마커 파일이 매번 재업로드되던 원인.
    """
    engine = _make_engine()
    # 로컬·리모트·state 모두 0바이트로 일치된 상태
    prior = FileState(
        local_size=0,
        local_mtime="2026-04-23T10:00:00Z",
        remote_size=0,
        remote_mtime="2026-04-23T10:00:00Z",
        remote_md5="d41d8cd98f00b204e9800998ecf8427e",   # 빈 파일 md5
    )
    actions = engine._decide_actions(
        local={"empty.txt": _local("empty.txt", size=0,
                                    mtime="2026-04-23T10:00:00Z",
                                    md5="d41d8cd98f00b204e9800998ecf8427e")},
        remote={"empty.txt": _remote("empty.txt", size=0,
                                      mtime="2026-04-23T10:00:00Z",
                                      md5="d41d8cd98f00b204e9800998ecf8427e")},
        state=SyncState(files={"empty.txt": prior}),
    )
    assert actions[0].type == ActionType.SKIP_SAME, (
        f"0바이트 파일은 SKIP_SAME 이어야 하는데 {actions[0].type} 됨"
    )


# ──────────────────────────────────────────────────────────
# rename/이동 감지 (delete+new → move 변환)
# ──────────────────────────────────────────────────────────

def _prior_for(size=100, mtime="2026-04-15T10:00:00Z", md5="abc"):
    return FileState(
        local_size=size, local_mtime=mtime, local_md5=md5,
        remote_size=size, remote_mtime=mtime, remote_md5=md5,
        remote_id="id-old",
    )


def test_local_rename_detected_as_move_remote():
    """로컬 폴더/파일 rename → DELETE_REMOTE+UPLOAD_NEW 대신 MOVE_REMOTE 1건."""
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"새폴더/a.txt": _local("새폴더/a.txt", size=100, md5="abc")},
        remote={"옛폴더/a.txt": _remote("옛폴더/a.txt", size=100, md5="abc")},
        state=SyncState(files={"옛폴더/a.txt": _prior_for()}),
    )
    assert len(actions) == 1
    assert actions[0].type == ActionType.MOVE_REMOTE
    assert actions[0].rel_path == "새폴더/a.txt"
    assert actions[0].move_from == "옛폴더/a.txt"


def test_rename_not_matched_when_content_differs():
    """내용(md5)이 다르면 이동으로 오판하지 않고 기존 삭제+업로드 유지."""
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"새폴더/a.txt": _local("새폴더/a.txt", size=100, md5="DIFFERENT")},
        remote={"옛폴더/a.txt": _remote("옛폴더/a.txt", size=100, md5="abc")},
        state=SyncState(files={"옛폴더/a.txt": _prior_for()}),
    )
    types = {a.type for a in actions}
    assert types == {ActionType.UPLOAD_NEW, ActionType.DELETE_REMOTE}


def test_zero_byte_files_not_matched_as_rename():
    """0바이트 파일은 md5 가 전부 같아 짝이 모호 → 이동 감지에서 제외."""
    engine = _make_engine()
    empty_md5 = "d41d8cd98f00b204e9800998ecf8427e"
    actions = engine._decide_actions(
        local={"새/empty.txt": _local("새/empty.txt", size=0, md5=empty_md5)},
        remote={"옛/empty.txt": _remote("옛/empty.txt", size=0, md5=empty_md5)},
        state=SyncState(files={"옛/empty.txt": _prior_for(size=0, md5=empty_md5)}),
    )
    types = {a.type for a in actions}
    assert ActionType.MOVE_REMOTE not in types
    assert ActionType.MOVE_LOCAL not in types


def test_remote_rename_detected_as_move_local():
    """Drive 쪽 rename → DELETE_LOCAL+DOWNLOAD_NEW 대신 MOVE_LOCAL 1건."""
    engine = _make_engine()
    actions = engine._decide_actions(
        local={"옛폴더/a.txt": _local("옛폴더/a.txt", size=100, md5="abc")},
        remote={"새폴더/a.txt": _remote("새폴더/a.txt", size=100, md5="abc")},
        state=SyncState(files={"옛폴더/a.txt": _prior_for()}),
    )
    assert len(actions) == 1
    assert actions[0].type == ActionType.MOVE_LOCAL
    assert actions[0].rel_path == "새폴더/a.txt"
    assert actions[0].move_from == "옛폴더/a.txt"


def test_folder_rename_converts_all_files_to_moves():
    """폴더 rename = 파일 여러 개 — 전부 짝이 맞아 이동으로 변환돼야 함."""
    engine = _make_engine()
    local = {}
    remote = {}
    priors = {}
    for i in range(3):
        rel_old = f"옛폴더/f{i}.txt"
        rel_new = f"새폴더/f{i}.txt"
        local[rel_new] = _local(rel_new, size=100 + i, md5=f"md5-{i}")
        remote[rel_old] = _remote(rel_old, size=100 + i, md5=f"md5-{i}")
        priors[rel_old] = _prior_for(size=100 + i, md5=f"md5-{i}")
    actions = engine._decide_actions(
        local=local, remote=remote, state=SyncState(files=priors),
    )
    assert all(a.type == ActionType.MOVE_REMOTE for a in actions)
    assert len(actions) == 3


def test_move_remote_execution_updates_state_and_prune_candidates():
    """MOVE_REMOTE 실행: 서버측 이동 호출 + state 이전 + 옛 폴더 정리 후보 등록."""
    from gdrive_sync.sync_engine import Action, SyncSummary

    drive = MagicMock()
    drive.resolve_folder_path.return_value = "new-parent-id"
    moved = _remote("새폴더/a.txt", size=100, md5="abc")
    drive.move_file.return_value = moved

    cfg = Config(conflict_policy="newer_wins")
    engine = SyncEngine(cfg=cfg, drive=drive, dry_run=False)
    pair = SyncPair(local_path=Path("/tmp"), remote_path="업무")
    old_rf = _remote("옛폴더/a.txt", size=100, md5="abc")
    old_rf.parents = ["old-parent-id"]
    lf = _local("새폴더/a.txt", size=100, md5="abc")
    state = SyncState(files={"옛폴더/a.txt": _prior_for()})
    summary = SyncSummary()

    action = Action(ActionType.MOVE_REMOTE, "새폴더/a.txt",
                    local=lf, remote=old_rf, move_from="옛폴더/a.txt")
    engine._execute_sequential_one(pair, action, "root-id", state, summary)

    assert summary.moved_remote == 1
    assert summary.errors == 0
    # modifiedTime 보존 요청 (rename 부작용 방지)
    kwargs = drive.move_file.call_args.kwargs
    assert kwargs["keep_modified_time"] == old_rf.modified_time
    assert kwargs["new_parent_id"] == "new-parent-id"
    assert kwargs["old_parent_id"] == "old-parent-id"
    # state 이전
    assert "옛폴더/a.txt" not in state.files
    assert state.files["새폴더/a.txt"].remote_id == moved.id
    # 옛 상위 폴더가 Drive 빈폴더 정리 후보로 등록됨
    assert "옛폴더" in engine._remote_prune_rels


def test_move_local_execution_moves_file_and_prunes(tmp_path):
    """MOVE_LOCAL 실행: 실제 파일 이동 + 옛 빈 폴더 정리 + state 이전."""
    from gdrive_sync.sync_engine import Action, SyncSummary

    old_dir = tmp_path / "옛폴더"
    old_dir.mkdir()
    src = old_dir / "a.txt"
    src.write_text("content")
    lf = _local("옛폴더/a.txt", size=7, md5="abc")
    lf.abs_path = src
    rf = _remote("새폴더/a.txt", size=7, md5="abc")

    cfg = Config(conflict_policy="newer_wins")
    engine = SyncEngine(cfg=cfg, drive=MagicMock(), dry_run=False)
    pair = SyncPair(local_path=tmp_path, remote_path="업무")
    state = SyncState(files={"옛폴더/a.txt": _prior_for(size=7)})
    summary = SyncSummary()

    action = Action(ActionType.MOVE_LOCAL, "새폴더/a.txt",
                    local=lf, remote=rf, move_from="옛폴더/a.txt")
    engine._execute_sequential_one(pair, action, "root-id", state, summary)

    assert summary.moved_local == 1
    assert (tmp_path / "새폴더" / "a.txt").read_text() == "content"
    assert not src.exists()
    assert not old_dir.exists()          # 비게 된 옛 폴더 자동 정리
    assert summary.pruned_dirs == 1
    assert "옛폴더/a.txt" not in state.files
    assert state.files["새폴더/a.txt"].remote_id == rf.id


def test_move_local_skips_when_dest_exists(tmp_path):
    """이동 목적지에 파일이 이미 있으면 덮어쓰지 않고 생략."""
    from gdrive_sync.sync_engine import Action, SyncSummary

    (tmp_path / "옛폴더").mkdir()
    src = tmp_path / "옛폴더" / "a.txt"
    src.write_text("old")
    (tmp_path / "새폴더").mkdir()
    (tmp_path / "새폴더" / "a.txt").write_text("existing")
    lf = _local("옛폴더/a.txt", size=3, md5="abc")
    lf.abs_path = src

    engine = SyncEngine(cfg=Config(), drive=MagicMock(), dry_run=False)
    pair = SyncPair(local_path=tmp_path, remote_path="업무")
    summary = SyncSummary()
    action = Action(ActionType.MOVE_LOCAL, "새폴더/a.txt",
                    local=lf, remote=_remote("새폴더/a.txt", size=3),
                    move_from="옛폴더/a.txt")
    engine._execute_sequential_one(pair, action, "root", SyncState(), summary)

    assert summary.moved_local == 0
    assert src.exists()  # 원본 보존
    assert (tmp_path / "새폴더" / "a.txt").read_text() == "existing"


# ──────────────────────────────────────────────────────────
# Drive 빈 폴더 자동 정리 (_prune_empty_remote_dirs)
# ──────────────────────────────────────────────────────────

def _prune_engine(policy="trash"):
    from gdrive_sync.sync_engine import SyncSummary
    drive = MagicMock()
    cfg = Config(conflict_policy="newer_wins", delete_policy=policy)
    engine = SyncEngine(cfg=cfg, drive=drive, dry_run=False)
    pair = SyncPair(local_path=Path("/tmp"), remote_path="업무")
    return engine, drive, pair, SyncSummary()


def test_prune_remote_dirs_removes_empty_chain():
    """빈 폴더를 bottom-up 으로 정리하되 페어 루트는 절대 안 건드림."""
    engine, drive, pair, summary = _prune_engine()
    drive.resolve_folder_path.side_effect = lambda p, create_missing: f"id:{p}"
    drive.folder_has_children.return_value = False
    engine._remote_prune_rels = {"옛폴더/하위"}

    engine._prune_empty_remote_dirs(pair, summary)

    deleted = [c.args[0] for c in drive.delete_file.call_args_list]
    assert deleted == ["id:업무/옛폴더/하위", "id:업무/옛폴더"]
    assert summary.pruned_remote_dirs == 2
    # trash 모드 → permanent=False
    assert all(c.kwargs.get("permanent") is False for c in drive.delete_file.call_args_list)
    # 캐시 무효화
    invalidated = [c.args[0] for c in drive.invalidate_cached_path.call_args_list]
    assert "업무/옛폴더/하위" in invalidated and "업무/옛폴더" in invalidated


def test_prune_remote_dirs_stops_at_nonempty():
    """자식이 남은 폴더는 보존하고 그 위로 올라가지 않음."""
    engine, drive, pair, summary = _prune_engine()
    drive.resolve_folder_path.side_effect = lambda p, create_missing: f"id:{p}"
    drive.folder_has_children.return_value = True
    engine._remote_prune_rels = {"옛폴더/하위"}

    engine._prune_empty_remote_dirs(pair, summary)

    drive.delete_file.assert_not_called()
    assert summary.pruned_remote_dirs == 0


def test_prune_remote_dirs_respects_skip_policy():
    """delete_policy=skip 이면 Drive 폴더도 정리하지 않음."""
    engine, drive, pair, summary = _prune_engine(policy="skip")
    engine._remote_prune_rels = {"옛폴더"}

    engine._prune_empty_remote_dirs(pair, summary)

    drive.resolve_folder_path.assert_not_called()
    drive.delete_file.assert_not_called()


def test_delete_remote_registers_prune_candidate():
    """DELETE_REMOTE 성공 시 상위 폴더가 정리 후보로 등록됨."""
    from gdrive_sync.sync_engine import Action, SyncSummary
    engine, drive, pair, summary = _prune_engine()
    state = SyncState(files={"옛폴더/a.txt": _prior_for()})
    action = Action(ActionType.DELETE_REMOTE, "옛폴더/a.txt",
                    None, _remote("옛폴더/a.txt"), _prior_for())

    engine._execute_sequential_one(pair, action, "root", state, summary)

    assert summary.deleted_remote == 1
    assert "옛폴더" in engine._remote_prune_rels


def test_force_download_overrides():
    engine = SyncEngine(
        cfg=Config(),
        drive=MagicMock(), dry_run=True,
        force_mode="download",
    )
    actions = engine._decide_actions(
        local={"a.txt": _local("a.txt")},
        remote={"a.txt": _remote("a.txt", md5="different")},
        state=SyncState(),
    )
    # force_download는 모든 리모트를 다운로드
    assert actions[0].type == ActionType.DOWNLOAD_UPDATE
