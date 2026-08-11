"""유니코드 파일명 정규화 (NFD → NFC).

macOS에서 생성된 파일은 한글이 NFD(자모 분해)로 저장되어
Windows(NFC)에서 'ㅈㅜ식포트풀리오'처럼 분절돼 보이는 문제를 해결.

- bottom-up 순회로 디렉토리도 안전하게 이름 변경
- 충돌(이미 NFC 이름이 존재) 시 스킵하고 보고
- dry_run 모드 지원
"""

from __future__ import annotations

import hashlib
import logging
import os
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from gdrive_sync.utils import to_long_path


log = logging.getLogger(__name__)


@dataclass
class NormalizeReport:
    scanned: int = 0          # 검사한 항목 총수
    needs_fix: int = 0        # 분절된(NFC ≠ 원본) 항목 수
    renamed: int = 0          # 실제 이름이 바뀐 수
    skipped_conflict: int = 0 # 내용 다른 충돌로 스킵
    deduped: int = 0          # NFD 중복파일 자동 삭제 (NFC와 내용 동일)
    errors: int = 0
    changes: list[tuple[Path, Path]] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)
    error_paths: list[tuple[Path, str]] = field(default_factory=list)


def is_decomposed(name: str) -> bool:
    """파일명이 NFD(분해형)이면 True."""
    return unicodedata.normalize("NFC", name) != name


def _md5(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.md5()
    with open(to_long_path(path), "rb") as f:
        while data := f.read(chunk):
            h.update(data)
    return h.hexdigest()


def normalize_path(
    root: Path,
    dry_run: bool = False,
    progress: Optional[Callable[[Path, str], None]] = None,
) -> NormalizeReport:
    """root 하위의 모든 파일/폴더 이름을 NFC로 정규화.

    - bottom-up 순서: 자식부터 처리해야 부모 rename 후 경로 깨짐 없음
    - dry_run=True: 변경 없이 리포트만 작성
    - progress(path, action): action ∈ {"scan","rename","skip","error"}
    """
    rep = NormalizeReport()
    root = Path(root)
    if not root.exists():
        rep.errors += 1
        rep.error_paths.append((root, "경로 없음"))
        return rep

    # bottom-up: topdown=False
    for cur_dir, dirnames, filenames in os.walk(to_long_path(root), topdown=False):
        cur_path = Path(cur_dir)

        # 파일부터 처리
        for fname in filenames:
            rep.scanned += 1
            if progress:
                progress(cur_path / fname, "scan")
            if not is_decomposed(fname):
                continue
            rep.needs_fix += 1
            new_name = unicodedata.normalize("NFC", fname)
            src = cur_path / fname
            dst = cur_path / new_name
            try:
                if dst.exists() and src.resolve() != dst.resolve():
                    # NFC 파일이 이미 별도 존재 — 내용 비교로 진짜 중복인지 판별
                    try:
                        same = (src.stat().st_size == dst.stat().st_size
                                and _md5(src) == _md5(dst))
                    except OSError:
                        same = False
                    if same:
                        # 내용 동일: NFD 사본은 NFC 원본의 중복 → 삭제
                        if not dry_run:
                            os.remove(to_long_path(src))
                        rep.deduped += 1
                        rep.changes.append((src, dst))
                        if progress:
                            progress(src, "rename")
                        log.info("NFD 중복 제거 (NFC 동일): %s", src.name)
                    else:
                        # 내용 다름: 진짜 충돌 — 건드리지 않고 경고
                        rep.skipped_conflict += 1
                        rep.conflicts.append(src)
                        if progress:
                            progress(src, "skip")
                        log.warning("충돌 스킵 (내용 다름): %s → %s", src, dst)
                    continue
                if not dry_run:
                    os.rename(to_long_path(src), to_long_path(dst))
                rep.renamed += 1
                rep.changes.append((src, dst))
                if progress:
                    progress(dst, "rename")
            except OSError as e:
                rep.errors += 1
                rep.error_paths.append((src, str(e)))
                if progress:
                    progress(src, "error")
                log.error("이름 변경 실패: %s → %s (%s)", src, dst, e)

        # 디렉토리 처리 (마지막에 자기 디렉토리)
        for dname in dirnames:
            rep.scanned += 1
            if progress:
                progress(cur_path / dname, "scan")
            if not is_decomposed(dname):
                continue
            rep.needs_fix += 1
            new_name = unicodedata.normalize("NFC", dname)
            src = cur_path / dname
            dst = cur_path / new_name
            try:
                if dst.exists() and src.resolve() != dst.resolve():
                    # 디렉토리는 내용 비교 없이 항상 충돌 경고 (안전 우선)
                    rep.skipped_conflict += 1
                    rep.conflicts.append(src)
                    if progress:
                        progress(src, "skip")
                    log.warning("충돌 스킵 (디렉토리 대상 존재): %s → %s", src, dst)
                    continue
                if not dry_run:
                    os.rename(to_long_path(src), to_long_path(dst))
                rep.renamed += 1
                rep.changes.append((src, dst))
                if progress:
                    progress(dst, "rename")
            except OSError as e:
                rep.errors += 1
                rep.error_paths.append((src, str(e)))
                if progress:
                    progress(src, "error")
                log.error("디렉토리 이름 변경 실패: %s → %s (%s)", src, dst, e)

    return rep


# ──────────────────────────────────────────────────────────
# Drive(리모트) 측 정규화
#
# 맥에서 브라우저로 Drive에 직접 업로드하면 NFD 이름 그대로 저장돼
# (GUI 동기화의 로컬 정규화를 안 거침) Windows 로 내려올 때 분절됨.
# Drive 서버측 rename(재전송 0바이트, modifiedTime 보존)으로 원천 수정.
# ──────────────────────────────────────────────────────────

@dataclass
class RemoteNormalizeReport:
    scanned: int = 0
    needs_fix: int = 0
    renamed: int = 0
    skipped_conflict: int = 0
    errors: int = 0
    changes: list[tuple[str, str]] = field(default_factory=list)   # (old_rel, new_rel)
    conflicts: list[str] = field(default_factory=list)
    # rel 재매핑용 — 성공적으로 rename 된 원본 rel 집합
    renamed_folders: set[str] = field(default_factory=set)
    renamed_files: set[str] = field(default_factory=set)


def normalize_remote_entries(
    drive,
    entries: list,          # list[(rel, DriveFile)] — 폴더+파일 모두
    dry_run: bool = False,
) -> RemoteNormalizeReport:
    """스캔된 Drive 항목 중 NFD 이름을 서버측 rename 으로 NFC 정규화.

    - Drive 는 같은 폴더에 동명 항목을 허용하므로, rename 전에 스캔 결과로
      NFC 동명 존재 여부를 확인해 중복 생성을 방지 (충돌 시 스킵+경고).
    - 파일은 modifiedTime 을 보존해 다른 PC 가 '변경'으로 오판하지 않게 함.
    - rename 은 id 기반이라 순서 무관 — 로그 가독성을 위해 폴더(얕은 것부터)
      → 파일 순으로 처리.
    """
    rep = RemoteNormalizeReport()
    all_rels = {rel for rel, _ in entries}
    ordered = sorted(entries, key=lambda e: (0 if e[1].is_folder else 1, e[0].count("/")))

    for rel, df in ordered:
        rep.scanned += 1
        if not is_decomposed(df.name):
            continue
        rep.needs_fix += 1
        nfc = unicodedata.normalize("NFC", df.name)
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        target = f"{parent}/{nfc}" if parent else nfc
        if target in all_rels:
            rep.skipped_conflict += 1
            rep.conflicts.append(rel)
            log.warning("Drive NFC 동명 항목 존재 — 스킵: %s", rel)
            continue
        if not dry_run:
            try:
                drive.move_file(
                    df.id,
                    new_name=nfc,
                    # 폴더의 modifiedTime 은 동기화 판정에 안 쓰이므로 파일만 보존
                    keep_modified_time=(df.modified_time if not df.is_folder else ""),
                )
            except Exception as e:
                rep.errors += 1
                log.error("Drive 이름 정규화 실패: %s (%s)", rel, e)
                continue
            df.name = nfc
        rep.renamed += 1
        rep.changes.append((rel, target))
        all_rels.add(target)   # 두 NFD 변형이 같은 NFC 로 겹치는 경우 방지
        (rep.renamed_folders if df.is_folder else rep.renamed_files).add(rel)
        tag = "[DRY-RUN] " if dry_run else ""
        log.info("🔤 %sDrive 이름 정규화: %s → %s", tag, rel, target)
    return rep


def remap_remote_rel(
    rel: str,
    renamed_folders: set[str],
    renamed_files: set[str],
) -> str:
    """rename 성공한 구간만 NFC 로 바꾼 새 rel 경로 계산.

    충돌/오류로 rename 못 한 구간은 원본 그대로 둬야 이후 diff 가
    실제 Drive 상태와 일치한다. prefix 비교는 항상 원본 rel 공간에서 수행.
    """
    parts = rel.split("/")
    out: list[str] = []
    prefix = ""
    for i, seg in enumerate(parts):
        prefix = seg if not prefix else f"{prefix}/{seg}"
        if i == len(parts) - 1:
            out.append(unicodedata.normalize("NFC", seg) if rel in renamed_files else seg)
        else:
            out.append(unicodedata.normalize("NFC", seg) if prefix in renamed_folders else seg)
    return "/".join(out)


def normalize_pairs(
    local_paths: list[Path],
    dry_run: bool = False,
    progress: Optional[Callable[[Path, str], None]] = None,
) -> dict[Path, NormalizeReport]:
    """여러 동기화 폴더에 대해 일괄 정규화."""
    out: dict[Path, NormalizeReport] = {}
    for p in local_paths:
        log.info("정규화 시작: %s", p)
        out[p] = normalize_path(p, dry_run=dry_run, progress=progress)
    return out
