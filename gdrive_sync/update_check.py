"""GitHub 저장소 기반 새 버전 확인.

배포 모델 (A안): 받는 사람은 `pip install git+https://github.com/...` 로 설치하고,
개발자가 git tag(v0.2.0 형식)를 push 하면 각 PC 에서 새 버전을 감지해 알린다.

버전 조회는 두 경로를 순서대로 시도:
1. GitHub REST API `/repos/{slug}/tags` — 저장소가 public 일 때 (인증 불필요)
2. `git ls-remote --tags` — private 저장소 폴백. pip 설치 시 사용한 Git 자격증명
   (Git Credential Manager)이 캐시돼 있으면 그대로 통과. GIT_TERMINAL_PROMPT=0
   으로 자격증명 프롬프트가 뜨지 않게 해 백그라운드 확인이 멈추지 않도록 한다.

확인 결과는 ~/.gdrive_sync/update_check.json 에 캐시하고 기본 24시간에 1번만
네트워크에 나간다 (force=True 로 무시 가능).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import __version__
from .config import DEFAULT_CONFIG_DIR

log = logging.getLogger(__name__)

REPO_SLUG = "GYcodeAI/gdrive-sync"
REPO_URL = f"https://github.com/{REPO_SLUG}.git"
PIP_SPEC = f"git+{REPO_URL}"

CACHE_PATH = DEFAULT_CONFIG_DIR / "update_check.json"
CHECK_INTERVAL_SEC = 24 * 3600

# v1.2.3 / 1.2.3 / v1.2 형태 태그만 버전으로 인정
_TAG_RE = re.compile(r"^v?(\d+(?:\.\d+)*)$")


@dataclass
class UpdateInfo:
    current: str
    latest: str

    @property
    def available(self) -> bool:
        return parse_version(self.latest) > parse_version(self.current)

    def upgrade_command(self) -> str:
        """받는 사람이 실행할 업그레이드 명령 (안내 문구용)."""
        return f"pip install --upgrade {PIP_SPEC}"


def parse_version(s: str) -> tuple:
    """'v1.2.3' → (1, 2, 3). 파싱 불가 조각은 무시하고 숫자만 비교."""
    m = _TAG_RE.match(s.strip())
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).split("."))


def _latest_from_tags(tags: list) -> Optional[str]:
    versioned = [(parse_version(t), t) for t in tags]
    versioned = [(v, t) for v, t in versioned if v]
    if not versioned:
        return None
    return max(versioned)[1]


# ──────────────────────────────────────────────────────────
# 원격 조회 (2단계 폴백)
# ──────────────────────────────────────────────────────────

def _fetch_tags_github_api(timeout: float) -> Optional[list]:
    """public 저장소: GitHub REST API 로 태그 목록. 실패/비공개(404)면 None."""
    url = f"https://api.github.com/repos/{REPO_SLUG}/tags?per_page=100"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"gdrive-sync/{__version__}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [t.get("name", "") for t in data if isinstance(t, dict)]
    except Exception as e:
        log.debug(f"GitHub API 태그 조회 실패 (폴백 진행): {e}")
        return None


def _fetch_tags_git_ls_remote(timeout: float) -> Optional[list]:
    """private 저장소 폴백: git ls-remote --tags (캐시된 자격증명 사용)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", REPO_URL],
            capture_output=True, text=True, timeout=timeout, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if result.returncode != 0:
            log.debug(f"git ls-remote 실패: {result.stderr.strip()[:200]}")
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        log.debug(f"git ls-remote 실행 불가: {e}")
        return None

    # 출력 형식: "<sha>\trefs/tags/v0.2.0" (주석 참조 ^{} 는 제외)
    tags = []
    for line in result.stdout.splitlines():
        parts = line.split("refs/tags/")
        if len(parts) == 2 and not parts[1].endswith("^{}"):
            tags.append(parts[1].strip())
    return tags


def fetch_latest_version(timeout: float = 5.0) -> Optional[str]:
    """원격의 최신 버전 태그 조회. 네트워크 불가 등 모든 실패 시 None."""
    tags = _fetch_tags_github_api(timeout)
    if tags is None:
        tags = _fetch_tags_git_ls_remote(timeout * 2)
    if tags is None:
        return None
    return _latest_from_tags(tags)


# ──────────────────────────────────────────────────────────
# 스로틀 캐시
# ──────────────────────────────────────────────────────────

def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(data: dict) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug(f"업데이트 캐시 저장 실패 (무시): {e}")


def check_for_update(force: bool = False, timeout: float = 5.0) -> Optional[UpdateInfo]:
    """새 버전 확인. 반환 None 은 '확인 불가 또는 스로틀 중 + 캐시에도 새 버전 없음'.

    force=False 면 24시간에 1번만 네트워크 조회하고, 그 사이에는 직전 조회에서
    알게 된 최신 버전으로만 판단한다. 예외를 던지지 않는다.
    """
    cache = _load_cache()
    now = time.time()
    latest: Optional[str] = None

    fresh = (now - cache.get("checked_at", 0)) < CHECK_INTERVAL_SEC
    if not force and fresh:
        latest = cache.get("latest")
    else:
        latest = fetch_latest_version(timeout=timeout)
        if latest is not None:
            _save_cache({"checked_at": now, "latest": latest})
        else:
            # 조회 실패: 재시도 폭주를 막기 위해 시각만 갱신, 이전 latest 유지
            _save_cache({"checked_at": now, "latest": cache.get("latest")})
            latest = cache.get("latest")

    if not latest:
        return None
    return UpdateInfo(current=__version__, latest=latest)


def run_pip_upgrade(quiet: bool = False) -> tuple:
    """현재 파이썬 환경에 pip 업그레이드 실행. 반환: (종료코드, 출력).

    quiet=True: 출력을 캡처하고 콘솔 창을 띄우지 않음 (GUI용).
    quiet=False: pip 출력을 그대로 콘솔에 스트리밍 (CLI용, 출력은 빈 문자열).
    """
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", PIP_SPEC]
    log.info("업그레이드 실행: %s", " ".join(cmd))
    if not quiet:
        return subprocess.call(cmd), ""
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")
