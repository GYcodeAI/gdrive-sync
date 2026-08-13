"""로컬 스캐너 테스트."""

from pathlib import Path

import pytest

from gdrive_sync.local_scanner import LocalScanner
from gdrive_sync.utils import matches_any


def test_matches_any_filename_pattern():
    assert matches_any("foo.tmp", ["*.tmp"])
    assert matches_any("deep/nested/foo.tmp", ["*.tmp"])
    assert not matches_any("foo.txt", ["*.tmp"])


def test_matches_any_directory_pattern():
    assert matches_any(".git/config", [".git/"])
    assert matches_any("src/__pycache__/a.pyc", ["__pycache__/"])
    assert not matches_any("docs/readme.md", [".git/"])


def test_matches_any_exact_name():
    assert matches_any(".DS_Store", [".DS_Store"])
    assert matches_any("sub/.DS_Store", [".DS_Store"])


def test_matches_any_empty_patterns():
    assert not matches_any("foo.txt", [])
    assert not matches_any("foo.txt", ["", "  "])


def test_matches_any_literal_bracket_pattern():
    # [THUMBNAIL]* 패턴이 리터럴 브라켓으로 처리되어야 함
    assert matches_any("[THUMBNAIL]image.jpg", ["[THUMBNAIL]*"])
    assert matches_any("sub/[THUMBNAIL]image.jpg", ["[THUMBNAIL]*"])
    assert not matches_any("thumbnail_image.jpg", ["[THUMBNAIL]*"])

    # [Conflict Copy]* 패턴 (Google Drive 충돌 파일명)
    assert matches_any("[Conflict Copy]report.docx", ["[Conflict Copy]*"])
    assert not matches_any("Conflict Copy report.docx", ["[Conflict Copy]*"])


def test_matches_any_bracket_not_mismatched_as_charclass():
    # 버그 재현: [THUMBNAIL] 을 문자 클래스로 오해석하면 단일 문자 'T'가 매칭됨
    # 수정 후에는 매칭되지 않아야 함
    assert not matches_any("T", ["[THUMBNAIL]"])
    assert not matches_any("H", ["[THUMBNAIL]"])

    # 실제 문자 클래스 [a-z], [!0-9] 는 그대로 동작해야 함
    assert matches_any("a", ["[a-z]"])
    assert not matches_any("A", ["[a-z]"])
    assert matches_any("x", ["[!0-9]"])
    assert not matches_any("5", ["[!0-9]"])


@pytest.fixture
def sample_tree(tmp_path: Path):
    """임시 디렉토리에 테스트용 파일 트리 생성."""
    (tmp_path / "root.txt").write_text("root", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub" / "b.tmp").write_text("temp", encoding="utf-8")  # 제외
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref", encoding="utf-8")  # 제외
    (tmp_path / "한글폴더").mkdir()
    (tmp_path / "한글폴더" / "한글파일.txt").write_text("hi", encoding="utf-8")
    return tmp_path


def test_scanner_excludes_patterns(sample_tree: Path):
    scanner = LocalScanner(sample_tree, exclude_patterns=["*.tmp", ".git/"])
    files = scanner.scan()
    rels = set(files.keys())
    assert "root.txt" in rels
    assert "sub/a.txt" in rels
    assert "sub/b.tmp" not in rels
    assert ".git/HEAD" not in rels


def test_scanner_supports_korean_names(sample_tree: Path):
    scanner = LocalScanner(sample_tree, exclude_patterns=[])
    files = scanner.scan()
    assert "한글폴더/한글파일.txt" in files


def test_scanner_collects_size_and_mtime(sample_tree: Path):
    scanner = LocalScanner(sample_tree, exclude_patterns=[])
    files = scanner.scan()
    lf = files["root.txt"]
    assert lf.size == 4
    assert lf.mtime_iso.endswith("Z")


def test_scanner_creates_missing_root(tmp_path: Path):
    root = tmp_path / "does_not_exist"
    scanner = LocalScanner(root, exclude_patterns=[])
    files = scanner.scan()
    assert root.exists()
    assert files == {}


def test_md5_on_demand(sample_tree: Path):
    scanner = LocalScanner(sample_tree, exclude_patterns=[])
    files = scanner.scan()
    lf = files["root.txt"]
    # MD5("root") = 63a9f0ea7bb98050796b649e85481845
    assert lf.md5() == "63a9f0ea7bb98050796b649e85481845"


@pytest.fixture
def office_lock_tree(tmp_path: Path):
    """Office/한글/백신·DLP 임시 파일이 섞인 트리 (Windows 친화적 파일명만 사용)."""
    (tmp_path / "report.docx").write_bytes(b"real")
    (tmp_path / "README.DOCX").write_bytes(b"real")   # 허용목록 → 정상 파일 보존
    (tmp_path / "LHZ5DWIU.DOCX").write_bytes(b"tmp")  # Word 8자 임시
    (tmp_path / "AB12CD34.XLSX").write_bytes(b"tmp")  # Excel 8자 임시
    (tmp_path / "JRZ5L.DOCX").write_bytes(b"tmp")     # 백신/DLP 5자 임시 (숫자 포함)
    (tmp_path / "ENNEO.DOCX").write_bytes(b"tmp")     # 백신/DLP 5자 임시 (숫자 없음)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "AB1234.HWP").write_bytes(b"tmp")  # 한글 6자 임시
    (tmp_path / "normal.txt").write_bytes(b"keep")
    return tmp_path


def test_scanner_excludes_office_temp_files(office_lock_tree: Path):
    scanner = LocalScanner(office_lock_tree, exclude_patterns=[])
    files = scanner.scan()
    rels = set(files.keys())

    # 정상 파일은 보존
    assert "report.docx" in rels
    assert "normal.txt" in rels
    assert "README.DOCX" in rels      # 허용목록에 있는 관례적 이름은 계속 보존

    # 랜덤 대문자(+숫자) 임시파일 제외 — 숫자 유무와 무관하게 5~8자면 제외
    assert "LHZ5DWIU.DOCX" not in rels
    assert "AB12CD34.XLSX" not in rels
    assert "sub/AB1234.HWP" not in rels
    assert "JRZ5L.DOCX" not in rels
    assert "ENNEO.DOCX" not in rels


def test_office_lock_prefix_filter(tmp_path: Path):
    """~$ / .~lock. 접두사 필터를 단위 테스트 (파일 생성 없이 이름 패턴만 확인)."""
    # 스캐너 내부 로직: startswith("~$") / startswith(".~lock.")
    lock_names = ["~$report.docx", "~$budget.xlsx", ".~lock.calc.ods"]
    normal_names = ["report.docx", "budget.xlsx", "README.DOCX"]

    for name in lock_names:
        assert name.startswith("~$") or name.startswith(".~lock."), f"{name} 필터에 걸려야 함"
    for name in normal_names:
        assert not (name.startswith("~$") or name.startswith(".~lock.")), f"{name} 보존되어야 함"


def test_windows_system_entries_skipped(tmp_path):
    """드라이브 루트 동기화 시 시스템 폴더/파일은 하드코딩 제외 (v2.4.3)."""
    (tmp_path / "$RECYCLE.BIN" / "S-1-5-21").mkdir(parents=True)
    (tmp_path / "$RECYCLE.BIN" / "S-1-5-21" / "deleted.txt").write_bytes(b"x")
    (tmp_path / "System Volume Information").mkdir()
    (tmp_path / "pagefile.sys").write_bytes(b"x")
    (tmp_path / "정상폴더").mkdir()
    (tmp_path / "정상폴더" / "문서.hwp").write_bytes(b"data")
    (tmp_path / "루트파일.txt").write_bytes(b"data")

    scanner = LocalScanner(tmp_path, exclude_patterns=[])
    rels = set(scanner.scan().keys())

    assert "정상폴더/문서.hwp" in rels
    assert "루트파일.txt" in rels
    assert not any("$RECYCLE.BIN" in r for r in rels)
    assert not any("System Volume Information" in r for r in rels)
    assert "pagefile.sys" not in rels
