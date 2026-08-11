"""Drive 쪽 NFD 파일명 정규화 테스트 (서버측 rename)."""

import unicodedata
from pathlib import Path
from unittest.mock import MagicMock

from gdrive_sync.config import Config, SyncPair
from gdrive_sync.drive_api import DriveFile, FOLDER_MIME
from gdrive_sync.normalize import (
    is_decomposed, normalize_remote_entries, remap_remote_rel,
)
from gdrive_sync.sync_engine import SyncEngine, SyncSummary

NFD = lambda s: unicodedata.normalize("NFD", s)   # noqa: E731
NFC = lambda s: unicodedata.normalize("NFC", s)   # noqa: E731


def _rfile(rel: str, name: str, mtime="2026-07-19T10:00:00Z") -> DriveFile:
    return DriveFile(
        id="id-" + rel.replace("/", "-"), name=name,
        mime_type="application/pdf", size=100,
        modified_time=mtime, md5="abc",
    )


def _rfolder(rel: str, name: str) -> DriveFile:
    return DriveFile(
        id="fid-" + rel.replace("/", "-"), name=name, mime_type=FOLDER_MIME,
    )


def test_normalize_remote_renames_nfd_file_preserving_mtime():
    drive = MagicMock()
    nfd_name = NFD("안건자료.pdf")
    df = _rfile(nfd_name, nfd_name)
    rep = normalize_remote_entries(drive, [(nfd_name, df)], dry_run=False)

    assert rep.needs_fix == 1 and rep.renamed == 1
    kwargs = drive.move_file.call_args.kwargs
    assert kwargs["new_name"] == NFC("안건자료.pdf")
    # 파일은 modifiedTime 보존 (다른 PC 가 변경으로 오판 방지)
    assert kwargs["keep_modified_time"] == "2026-07-19T10:00:00Z"
    assert df.name == NFC("안건자료.pdf")
    assert nfd_name in rep.renamed_files


def test_normalize_remote_renames_nfd_folder():
    drive = MagicMock()
    nfd = NFD("이사회")
    folder = _rfolder(nfd, nfd)
    rep = normalize_remote_entries(drive, [(nfd, folder)], dry_run=False)

    assert rep.renamed == 1
    assert nfd in rep.renamed_folders
    # 폴더는 modifiedTime 보존 안 함 (판정에 안 쓰임)
    assert drive.move_file.call_args.kwargs["keep_modified_time"] == ""


def test_normalize_remote_skips_when_nfc_sibling_exists():
    """같은 폴더에 NFC 동명 항목이 이미 있으면 중복 생성 방지 위해 스킵."""
    drive = MagicMock()
    nfd = NFD("보고서.pdf")
    entries = [
        (nfd, _rfile(nfd, nfd)),
        (NFC("보고서.pdf"), _rfile("nfc", NFC("보고서.pdf"))),
    ]
    rep = normalize_remote_entries(drive, entries, dry_run=False)

    assert rep.renamed == 0
    assert rep.skipped_conflict == 1
    drive.move_file.assert_not_called()


def test_normalize_remote_dry_run_no_api_calls():
    drive = MagicMock()
    nfd = NFD("안건.pdf")
    rep = normalize_remote_entries(drive, [(nfd, _rfile(nfd, nfd))], dry_run=True)

    assert rep.needs_fix == 1 and rep.renamed == 1   # 보고용 카운트
    drive.move_file.assert_not_called()


def test_normalize_remote_nfc_names_untouched():
    drive = MagicMock()
    name = NFC("정상파일.pdf")
    rep = normalize_remote_entries(drive, [(name, _rfile(name, name))], dry_run=False)
    assert rep.needs_fix == 0
    drive.move_file.assert_not_called()


def test_remap_remote_rel_folder_and_file():
    folder_nfd = NFD("이사회")
    file_nfd = f"{folder_nfd}/{NFD('안건.pdf')}"
    new = remap_remote_rel(file_nfd, {folder_nfd}, {file_nfd})
    assert new == f"{NFC('이사회')}/{NFC('안건.pdf')}"


def test_remap_remote_rel_keeps_failed_segments():
    """rename 실패한 구간은 원본 유지 — diff 가 실제 Drive 상태와 일치해야 함."""
    folder_nfd = NFD("이사회")
    file_nfd = f"{folder_nfd}/{NFD('안건.pdf')}"
    # 폴더만 rename 성공, 파일은 실패
    new = remap_remote_rel(file_nfd, {folder_nfd}, set())
    assert new == f"{NFC('이사회')}/{NFD('안건.pdf')}"


def test_engine_normalize_remote_remaps_rfiles_and_invalidates_cache():
    drive = MagicMock()
    cfg = Config(conflict_policy="newer_wins")
    cfg.auto_normalize_filenames = True
    engine = SyncEngine(cfg=cfg, drive=drive, dry_run=False)
    pair = SyncPair(local_path=Path("/tmp"), remote_path="업무")

    folder_nfd = NFD("이사회")
    file_rel = f"{folder_nfd}/{NFD('안건.pdf')}"
    rfolders = [(folder_nfd, _rfolder(folder_nfd, folder_nfd))]
    rfiles = {file_rel: _rfile(file_rel, NFD("안건.pdf"))}
    summary = SyncSummary()

    out = engine._normalize_remote_names(pair, rfolders, rfiles, summary)

    assert summary.normalized_remote == 2
    assert list(out.keys()) == [f"{NFC('이사회')}/{NFC('안건.pdf')}"]
    drive.invalidate_cached_path.assert_called_once_with(f"업무/{folder_nfd}")


def test_engine_normalize_remote_noop_when_all_nfc():
    """NFD 없으면 API 호출 0회 — 매 동기화 비용 없음."""
    drive = MagicMock()
    cfg = Config(conflict_policy="newer_wins")
    cfg.auto_normalize_filenames = True
    engine = SyncEngine(cfg=cfg, drive=drive, dry_run=False)
    pair = SyncPair(local_path=Path("/tmp"), remote_path="업무")

    name = NFC("정상.pdf")
    rfiles = {name: _rfile(name, name)}
    out = engine._normalize_remote_names(pair, [], rfiles, SyncSummary())

    assert out is rfiles
    drive.move_file.assert_not_called()
    drive.invalidate_cached_path.assert_not_called()
