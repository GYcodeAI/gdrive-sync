"""로컬 파일명 NFD→NFC 정규화 테스트.

v2.4.6 데이터 삭제 사고 회귀 방지:
APFS·HFS+ 같은 정규화 무시 파일시스템에서 NFD 이름 파일 하나만 있어도
NFC 경로 조회가 같은 파일에 닿아, 종전 resolve() 문자열 비교가 이를
'별도의 NFC 원본 존재'로 오판 → 자기 자신과 md5 비교(항상 동일) →
유일한 사본을 os.remove 로 삭제했다. samefile(inode) 판정으로 수정.
"""

import os
import unicodedata
from pathlib import Path

import pytest

from gdrive_sync.normalize import _same_entry, is_decomposed, normalize_path

NFD_NAME = unicodedata.normalize("NFD", "테스트문서.pptx")
NFC_NAME = unicodedata.normalize("NFC", "테스트문서.pptx")


def _fs_is_normalization_insensitive(tmp_path: Path) -> bool:
    """이 tmp_path 의 파일시스템이 NFD/NFC 를 같은 항목으로 취급하는지 탐지."""
    probe = tmp_path / "_probe"
    probe.mkdir()
    (probe / NFD_NAME).write_bytes(b"probe")
    try:
        nfc = probe / NFC_NAME
        return nfc.exists() and os.path.samefile(probe / NFD_NAME, nfc)
    finally:
        for f in probe.iterdir():
            f.unlink()
        probe.rmdir()


def test_nfd_only_file_is_renamed_not_deleted(tmp_path):
    """NFD 파일 하나뿐이면 삭제가 아니라 NFC rename 이어야 한다 (사고 재현 케이스)."""
    src = tmp_path / NFD_NAME
    src.write_bytes(b"important pptx content")

    rep = normalize_path(tmp_path)

    survivors = [n for n in os.listdir(tmp_path)]
    assert survivors, "파일이 삭제되면 안 된다"
    assert len(survivors) == 1
    assert rep.deduped == 0, "자기 자신을 중복으로 오판해 삭제하면 안 된다"
    assert rep.errors == 0
    assert (tmp_path / NFC_NAME).read_bytes() == b"important pptx content"
    # 정규화 무시 FS(APFS)에서도 저장된 이름 형태가 NFC 로 바뀌어야 한다
    if _fs_is_normalization_insensitive(tmp_path):
        assert unicodedata.is_normalized("NFC", survivors[0])
        assert rep.renamed == 1


def test_nfd_only_directory_is_renamed_not_conflict(tmp_path):
    """NFD 디렉토리 하나뿐이면 충돌 스킵이 아니라 rename 이어야 한다."""
    nfd_dir = unicodedata.normalize("NFD", "한글폴더")
    (tmp_path / nfd_dir).mkdir()
    (tmp_path / nfd_dir / "inner.txt").write_bytes(b"x")

    rep = normalize_path(tmp_path)

    assert rep.skipped_conflict == 0
    assert rep.errors == 0
    nfc_dir = tmp_path / unicodedata.normalize("NFC", "한글폴더")
    assert (nfc_dir / "inner.txt").read_bytes() == b"x"


def test_same_entry_detects_normalization_alias(tmp_path):
    """정규화 무시 FS 에서 NFD/NFC 경로가 같은 항목임을 감지해야 한다."""
    src = tmp_path / NFD_NAME
    src.write_bytes(b"data")
    dst = tmp_path / NFC_NAME
    if _fs_is_normalization_insensitive(tmp_path):
        assert _same_entry(src, dst) is True
    else:
        assert _same_entry(src, dst) is False  # dst 미존재 → OSError → False


def test_true_duplicate_still_deduped(tmp_path):
    """정규화 구분 FS 에서 내용 동일한 진짜 NFD/NFC 쌍은 종전대로 중복 제거."""
    if _fs_is_normalization_insensitive(tmp_path):
        pytest.skip("정규화 무시 FS 에서는 NFD/NFC 쌍을 별도 파일로 만들 수 없음")
    (tmp_path / NFD_NAME).write_bytes(b"same content")
    (tmp_path / NFC_NAME).write_bytes(b"same content")

    rep = normalize_path(tmp_path)

    assert rep.deduped == 1
    assert os.listdir(tmp_path) == [NFC_NAME]


def test_real_conflict_skipped(tmp_path):
    """정규화 구분 FS 에서 내용 다른 NFD/NFC 쌍은 건드리지 않고 충돌 보고."""
    if _fs_is_normalization_insensitive(tmp_path):
        pytest.skip("정규화 무시 FS 에서는 NFD/NFC 쌍을 별도 파일로 만들 수 없음")
    (tmp_path / NFD_NAME).write_bytes(b"content A")
    (tmp_path / NFC_NAME).write_bytes(b"content B")

    rep = normalize_path(tmp_path)

    assert rep.skipped_conflict == 1
    assert sorted(os.listdir(tmp_path)) == sorted([NFD_NAME, NFC_NAME])


def test_dry_run_touches_nothing(tmp_path):
    src = tmp_path / NFD_NAME
    src.write_bytes(b"data")

    rep = normalize_path(tmp_path, dry_run=True)

    assert rep.needs_fix == 1
    names = os.listdir(tmp_path)
    assert len(names) == 1
    assert is_decomposed(names[0]) or not _fs_is_normalization_insensitive(tmp_path)
    assert (tmp_path / NFD_NAME).read_bytes() == b"data"
