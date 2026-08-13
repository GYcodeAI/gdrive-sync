"""Google Drive API v3 래퍼.

- 파일/폴더 CRUD (list, create, update, delete, download, upload)
- 경로 → ID 변환 및 **스레드 안전 캐시** (병렬 업로드 시 폴더 중복 생성 방지)
- 재귀 트리 조회
- **v2 변경**:
  * 작은 파일(<5MB)은 simple upload (Google 공식 한도 준수)
  * 파일 크기별 청크 자동 선택
  * BandwidthLimiter 주입 지원 (청크마다 throttle)
  * 외부에서 credentials/path_cache 주입 가능 (병렬 워커용)
- **v2.1 패치 (2026-04-15)**:
  * httplib2의 308 Resume Incomplete 응답 처리 버그 우회
  * 중간 크기 파일(5~50MB)에서 'Redirected but missing Location: header'
    에러 발생 시 단일 청크 모드로 자동 재시도 (청크 분할 없음 → 308 안 뜸)
"""

from __future__ import annotations

import io
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import httplib2
from google.auth.transport.requests import Request
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from gdrive_sync.auth import load_credentials
from gdrive_sync.bandwidth import BandwidthLimiter
from gdrive_sync.config import NetworkConfig, PerformanceConfig, SIMPLE_UPLOAD_MAX
from gdrive_sync.network import build_http
from gdrive_sync.utils import split_remote_path


log = logging.getLogger(__name__)

# httplib2 버그 회피: resumable upload 중 Google이 반환하는 308 Resume Incomplete
# 응답을 httplib2가 "리다이렉트"로 오해해 RedirectMissingLocation / RedirectLimit
# 예외를 던지는 알려진 이슈. 이 예외를 transient로 간주하고 단일 청크 모드로 재시도.
_HTTPLIB2_REDIRECT_BUG = (
    httplib2.RedirectMissingLocation,
    httplib2.RedirectLimit,
)

# Drive API 는 호출 속도 제한을 429 뿐 아니라 403(userRateLimitExceeded 등)으로도
# 반환한다. 403 은 권한 오류와 구분해야 하므로 reason 을 확인해 rate limit 일 때만
# 재시도 대상으로 판정한다. (2026-08-13: 대량 병렬 업로드에서 403 즉시실패
# 19,180건 발생 사례 — v2.4.2)
_RATE_LIMIT_REASONS = frozenset({
    "userRateLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded",
})


def _is_rate_limit_403(e: HttpError) -> bool:
    """403 HttpError 가 속도 제한(재시도 가능)인지 판정."""
    try:
        for d in (getattr(e, "error_details", None) or []):
            if isinstance(d, dict) and d.get("reason") in _RATE_LIMIT_REASONS:
                return True
        # 구버전 googleapiclient 폴백: 응답 본문 JSON 의 errors[].reason
        import json
        data = json.loads(e.content.decode("utf-8"))
        for err in data.get("error", {}).get("errors", []):
            if err.get("reason") in _RATE_LIMIT_REASONS:
                return True
    except Exception:
        pass
    return False


def _is_retryable(e: HttpError, status: int) -> bool:
    """백오프 재시도 대상 HTTP 상태인지 판정 (403 은 rate limit 사유일 때만)."""
    if status in (429, 500, 502, 503, 504):
        return True
    return status == 403 and _is_rate_limit_403(e)

# Google Workspace 네이티브 mime — 동기화 제외
WORKSPACE_MIMES = frozenset({
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
    "application/vnd.google-apps.drawing",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.script",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.fusiontable",
    "application/vnd.google-apps.shortcut",
})

FOLDER_MIME = "application/vnd.google-apps.folder"

FILE_FIELDS = "id, name, mimeType, size, modifiedTime, md5Checksum, parents, trashed"
LIST_FIELDS = f"nextPageToken, files({FILE_FIELDS})"


@dataclass
class DriveFile:
    """Drive API 응답을 파싱한 경량 구조."""
    id: str
    name: str
    mime_type: str
    size: int = 0
    modified_time: str = ""     # RFC3339
    md5: str = ""
    parents: list[str] = field(default_factory=list)
    trashed: bool = False

    @property
    def is_folder(self) -> bool:
        return self.mime_type == FOLDER_MIME

    @property
    def is_workspace_native(self) -> bool:
        return self.mime_type in WORKSPACE_MIMES

    @classmethod
    def from_api(cls, data: dict) -> "DriveFile":
        return cls(
            id=data["id"],
            name=data.get("name", ""),
            mime_type=data.get("mimeType", ""),
            size=int(data.get("size") or 0),
            modified_time=data.get("modifiedTime", ""),
            md5=data.get("md5Checksum", "") or "",
            parents=list(data.get("parents") or []),
            trashed=bool(data.get("trashed", False)),
        )


# ──────────────────────────────────────────────────────────
# Drive 클라이언트
# ──────────────────────────────────────────────────────────

class DriveClient:
    """Google Drive API 래퍼.

    병렬 전송을 위해 외부에서 credentials / path_cache / path_cache_lock을
    주입할 수 있음. 단일 스레드 사용 시엔 None으로 둬도 자동 생성.
    """

    def __init__(
        self,
        net: NetworkConfig,
        interactive_auth: bool = False,
        credentials=None,
        path_cache: Optional[dict[str, str]] = None,
        path_cache_lock: Optional[threading.Lock] = None,
        performance: Optional[PerformanceConfig] = None,
        acknowledge_abuse: bool = False,
    ):
        self.net = net
        self.performance = performance or PerformanceConfig()
        # Drive가 '악성 의심'으로 분류해 차단한 파일도 다운로드 허용 여부
        # (본인 소유 파일에만 적용 가능. False 기본값 = 안전)
        self.acknowledge_abuse = acknowledge_abuse
        self._credentials = credentials or load_credentials(interactive=interactive_auth)

        http = build_http(net)
        authed_http = AuthorizedHttp(self._credentials, http=http)
        self.service = build(
            "drive", "v3",
            http=authed_http,
            cache_discovery=False,
        )

        # 경로 → folder id 캐시 (스레드 공유 가능)
        self._path_cache: dict[str, str] = (
            path_cache if path_cache is not None else {"": "root"}
        )
        self._path_cache_lock = path_cache_lock or threading.Lock()

    # ──────────────────────────────────────────────
    # 재시도 래퍼
    # ──────────────────────────────────────────────
    def _retry(self, fn: Callable, *, attempts: Optional[int] = None):
        max_retries = attempts or self.net.max_retries
        for i in range(max_retries + 1):
            try:
                return fn()
            except HttpError as e:
                status = getattr(e.resp, "status", 0)
                if _is_retryable(e, status) and i < max_retries:
                    wait = (2 ** i) + random.random()
                    log.warning(f"HTTP {status} — {wait:.1f}s 후 재시도 ({i+1}/{max_retries})")
                    time.sleep(wait)
                    continue
                raise
            except (OSError, ConnectionError) as e:
                if i < max_retries:
                    wait = (2 ** i) + random.random()
                    log.warning(f"네트워크 에러: {e} — {wait:.1f}s 후 재시도")
                    time.sleep(wait)
                    continue
                raise

    # ──────────────────────────────────────────────
    # 폴더 경로 → ID 변환 (스레드 안전)
    # ──────────────────────────────────────────────
    def resolve_folder_path(self, remote_path: str, create_missing: bool = True) -> str:
        """구글드라이브 상의 '업무/프로젝트' 경로를 폴더 ID로 변환.

        병렬 호출 안전: _path_cache_lock으로 보호.
        """
        remote_path = (remote_path or "").strip("/")
        if not remote_path:
            return "root"
        with self._path_cache_lock:
            cached = self._path_cache.get(remote_path)
        if cached:
            return cached

        parent_id = "root"
        built: list[str] = []
        for part in split_remote_path(remote_path):
            built.append(part)
            joined = "/".join(built)

            with self._path_cache_lock:
                cached = self._path_cache.get(joined)
            if cached:
                parent_id = cached
                continue

            # 캐시에 없는 세그먼트는 find+create+캐시등록을 한 lock 안에서 직렬화한다.
            # 그러지 않으면 두 스레드가 동시에 find→없음→create를 호출해
            # Drive에 동명 폴더 2개가 생기고 한쪽은 고아가 됨.
            with self._path_cache_lock:
                cached = self._path_cache.get(joined)
                if cached:
                    parent_id = cached
                    continue
                folder_id = self._find_child_folder(parent_id, part)
                if not folder_id:
                    if not create_missing:
                        raise FileNotFoundError(f"구글드라이브 폴더 없음: {joined}")
                    folder_id = self.create_folder(part, parent_id)
                    log.debug(f"폴더 생성: {joined}")
                self._path_cache[joined] = folder_id
                parent_id = folder_id

        return parent_id

    def _find_child_folder(self, parent_id: str, name: str) -> Optional[str]:
        q = (
            f"'{parent_id}' in parents and "
            f"mimeType = '{FOLDER_MIME}' and "
            f"name = '{_escape_q(name)}' and "
            f"trashed = false"
        )
        resp = self._retry(lambda: self.service.files().list(
            q=q,
            fields="files(id, name)",
            pageSize=1,
            spaces="drive",
            supportsAllDrives=False,
        ).execute())
        files = resp.get("files") or []
        return files[0]["id"] if files else None

    # ──────────────────────────────────────────────
    # 폴더 트리 재귀 조회
    # ──────────────────────────────────────────────
    def list_tree(
        self,
        folder_id: str,
        parallel: bool = True,
        max_workers: int = 8,
    ) -> Iterator[tuple[str, DriveFile]]:
        """folder_id를 루트로 하는 전체 트리를 (상대경로, DriveFile)로 yield.

        상대경로는 POSIX '/' 구분, folder_id 자신은 '' 빈 문자열.

        parallel=True: BFS 레벨별 병렬 조회 (스레드별 독립 service 사용).
        parallel=False: 직렬 재귀 (기존 동작).
        """
        if parallel:
            yield from self._list_tree_parallel(folder_id, max_workers=max_workers)
        else:
            yield from self._list_tree_inner(folder_id, "")

    def _list_tree_inner(self, folder_id: str, prefix: str) -> Iterator[tuple[str, DriveFile]]:
        for child in self.list_children(folder_id):
            if child.trashed:
                continue
            rel = f"{prefix}/{child.name}" if prefix else child.name
            yield rel, child
            if child.is_folder:
                yield from self._list_tree_inner(child.id, rel)

    def _list_tree_parallel(
        self,
        root_id: str,
        max_workers: int = 8,
    ) -> Iterator[tuple[str, DriveFile]]:
        """큐 기반 fully-concurrent 트리 조회.

        기존 BFS 레벨 배리어 제거 — 새 폴더가 발견되는 즉시 idle worker가
        집어가서 처리. 큰 폴더 하나가 다른 worker 들을 막지 않음.

        httplib2가 thread-safe 하지 않아 스레드별 독립 service 인스턴스 사용.
        credentials는 공유(이미 로딩됨).
        """
        import queue as _queue
        from concurrent.futures import ThreadPoolExecutor

        tls = threading.local()
        result_q: "_queue.Queue[object]" = _queue.Queue()
        # in-flight: 큐에 들어가 있거나 처리 중인 폴더 개수.
        # 0이 되면 트리 조회 완료 → SENTINEL 송출.
        counter_lock = threading.Lock()
        in_flight = [1]   # 루트 1개로 시작
        SENTINEL = object()

        def _get_thread_service():
            svc = getattr(tls, "service", None)
            if svc is None:
                http = build_http(self.net)
                authed = AuthorizedHttp(self._credentials, http=http)
                svc = build("drive", "v3", http=authed, cache_discovery=False)
                tls.service = svc
            return svc

        def _process(folder_id: str, prefix: str, executor: ThreadPoolExecutor) -> None:
            """한 폴더의 모든 페이지를 순차 조회. 자식 폴더는 즉시 submit."""
            try:
                svc = _get_thread_service()
                token: Optional[str] = None
                while True:
                    resp = self._retry(lambda: svc.files().list(
                        q=f"'{folder_id}' in parents and trashed = false",
                        fields=LIST_FIELDS,
                        pageSize=1000,
                        pageToken=token,
                        spaces="drive",
                        supportsAllDrives=False,
                    ).execute())
                    for item in resp.get("files") or []:
                        child = DriveFile.from_api(item)
                        if child.trashed:
                            continue
                        rel = f"{prefix}/{child.name}" if prefix else child.name
                        result_q.put((rel, child))
                        if child.is_folder:
                            with counter_lock:
                                in_flight[0] += 1
                            # 즉시 다른 worker 에게 위임
                            try:
                                executor.submit(_process, child.id, rel, executor)
                            except RuntimeError:
                                # executor shutdown 중이면 카운터만 정리
                                with counter_lock:
                                    in_flight[0] -= 1
                    token = resp.get("nextPageToken")
                    if not token:
                        break
            except Exception as e:
                log.warning(f"폴더 조회 실패 (건너뜀): {folder_id}: {e}")
            finally:
                with counter_lock:
                    in_flight[0] -= 1
                    done = (in_flight[0] == 0)
                if done:
                    result_q.put(SENTINEL)

        with ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="gdrv-list",
        ) as ex:
            ex.submit(_process, root_id, "", ex)
            while True:
                item = result_q.get()
                if item is SENTINEL:
                    return
                yield item   # type: ignore[misc]

    def list_children(self, folder_id: str) -> list[DriveFile]:
        q = f"'{folder_id}' in parents and trashed = false"
        results: list[DriveFile] = []
        token: Optional[str] = None
        while True:
            # pageSize=1000(API 최대) — 100보다 round-trip 10배 절감
            resp = self._retry(lambda: self.service.files().list(
                q=q,
                fields=LIST_FIELDS,
                pageSize=1000,
                pageToken=token,
                spaces="drive",
                supportsAllDrives=False,
            ).execute())
            for item in resp.get("files") or []:
                results.append(DriveFile.from_api(item))
            token = resp.get("nextPageToken")
            if not token:
                break
        return results

    # ──────────────────────────────────────────────
    # CRUD
    # ──────────────────────────────────────────────
    def create_folder(self, name: str, parent_id: str) -> str:
        body = {
            "name": name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }
        resp = self._retry(lambda: self.service.files().create(
            body=body, fields="id",
        ).execute())
        return resp["id"]

    # ──────────────────────────────────────────────
    # 업로드 — simple / resumable 자동 분기
    # ──────────────────────────────────────────────
    def upload_file(
        self,
        local_path: Path,
        parent_id: str,
        name: Optional[str] = None,
        existing_id: Optional[str] = None,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
        strategy: str = "auto",       # "auto" | "simple" | "resumable"
        stop_checker: Optional[Callable[[], bool]] = None,  # True면 즉시 중단
    ) -> DriveFile:
        """파일 업로드. existing_id가 있으면 해당 파일을 업데이트(새 버전).

        strategy="auto": 파일 크기 < 5MB이면 simple, 그 이상이면 resumable.
        stop_checker: 호출 시 True 반환하면 현재 청크 완료 후 중단 (InterruptedError).
        """
        name = name or local_path.name
        total = local_path.stat().st_size

        if strategy == "auto":
            strategy = "simple" if total < SIMPLE_UPLOAD_MAX else "resumable"

        if strategy == "simple":
            result = self._upload_simple(
                local_path, parent_id, name, existing_id,
                progress_cb, bandwidth_limiter, total,
            )
        else:
            result = self._upload_resumable(
                local_path, parent_id, name, existing_id,
                progress_cb, bandwidth_limiter, total,
                stop_checker=stop_checker,
            )
        return self._ensure_upload_metadata(result)

    def _ensure_upload_metadata(self, result: DriveFile) -> DriveFile:
        """업로드 응답에 md5Checksum/modifiedTime 이 빠졌으면 한 번 재조회해 보정.

        ⚠️ 영구 재전송 버그의 근본 원인 방지:
        대용량 resumable 업로드(특히 308 단일 PUT 폴백)의 최종 응답은
        요청한 fields(md5Checksum, modifiedTime)를 누락한 채 돌아오는 경우가 있다.
        이 빈 값이 그대로 state 에 저장되면:
          - force upload 모드 + ≥large_file_md5_skip_mb 파일 → 매번 재업로드
          - 양방향 모드 → 매번 재다운로드(동일 내용 덮어쓰기)
        둘 다 _remote_changed() 가 빈 prior.remote_md5/remote_mtime 때문에
        항상 True 로 오판하기 때문. files().get 으로 권위 있는 메타데이터를
        한 번 더 받아 저장값이 다음 스캔 결과와 일치하도록 한다.

        simple 업로드(작은 파일)는 응답에 md5 가 정상 포함되므로 재조회를
        타지 않는다(추가 RTT 없음). 누락된 대용량 파일만 1회 보정한다.
        """
        if result.md5 and result.modified_time:
            return result
        try:
            refetched = self.get_file(result.id)
        except Exception as e:
            log.warning(
                f"업로드 후 메타데이터 재조회 실패 (state 가 불완전할 수 있음) "
                f"{result.id}: {e}"
            )
            return result
        # 재조회로 md5/modifiedTime 가 채워졌으면 교체. 여전히 비어 있으면
        # (Google 측 지연 등) 원본 유지 — 무한 루프 방지 위해 1회만 시도.
        if refetched.md5 or refetched.modified_time:
            log.debug(
                f"업로드 응답 메타 누락 → 재조회 보정: {result.id} "
                f"(md5={'O' if refetched.md5 else 'X'}, "
                f"mtime={'O' if refetched.modified_time else 'X'})"
            )
            return refetched
        log.warning(
            f"업로드 후에도 md5/modifiedTime 미확정: {result.id} "
            f"— 다음 동기화에서 재전송될 수 있음 (Google 처리 지연 의심)"
        )
        return result

    def _upload_simple(
        self, local_path, parent_id, name, existing_id,
        progress_cb, bandwidth_limiter, total,
    ) -> DriveFile:
        """Simple upload: 한 번의 POST로 완료. 5MB 미만 전용."""
        # 전송 전 대역폭 토큰 확보 (전체 바이트 한번에)
        if bandwidth_limiter and total > 0:
            bandwidth_limiter.consume_upload(total)

        media = MediaFileUpload(str(local_path), resumable=False)
        if existing_id:
            request = self.service.files().update(
                fileId=existing_id,
                media_body=media,
                fields=FILE_FIELDS,
            )
        else:
            body = {"name": name, "parents": [parent_id]}
            request = self.service.files().create(
                body=body,
                media_body=media,
                fields=FILE_FIELDS,
            )
        response = self._retry(lambda: request.execute())
        if progress_cb:
            progress_cb(total, total)
        return DriveFile.from_api(response)

    def _upload_resumable(
        self, local_path, parent_id, name, existing_id,
        progress_cb, bandwidth_limiter, total,
        stop_checker=None,
    ) -> DriveFile:
        """Resumable upload 래퍼.

        1차: 정상 청크 단위 업로드 (빠름, 대용량 파일에 적합, 중단 재개 지원)
        2차(fallback): httplib2의 308 버그 감지 시 단일 청크로 재시도
        """
        primary_chunk = self._select_chunk_size(total, bandwidth_limiter)

        try:
            return self._upload_resumable_inner(
                local_path, parent_id, name, existing_id,
                progress_cb, bandwidth_limiter, total,
                chunk_size=primary_chunk,
                stop_checker=stop_checker,
            )
        except _HTTPLIB2_REDIRECT_BUG as e:
            size_mb = total / 1024 / 1024
            log.warning(
                f"⚠ 단일 청크 모드 — {local_path.name} ({size_mb:.1f} MB)"
            )
            log.warning(
                f"   httplib2 308 버그({type(e).__name__}) 감지 → "
                "전체 파일 한 번에 PUT 요청."
            )
            log.warning(
                "   대역폭 제한 적용: PUT 시작 전에 사전 대기로 throttle 보장 → 그다음 PUT은 풀스피드."
            )
            # 단일 청크 모드: chunksize=-1 → 전체를 한 번의 PUT으로 전송
            # 308 응답이 없으므로 httplib2 버그 회피
            return self._upload_resumable_inner(
                local_path, parent_id, name, existing_id,
                progress_cb, bandwidth_limiter, total,
                chunk_size=-1,
                stop_checker=stop_checker,
            )

    def _upload_resumable_inner(
        self, local_path, parent_id, name, existing_id,
        progress_cb, bandwidth_limiter, total,
        chunk_size: int,
        stop_checker=None,
    ) -> DriveFile:
        """실제 resumable upload 루프.

        chunk_size = -1 이면 전체 파일을 한 번의 PUT으로 전송 (단일 청크 모드).
        stop_checker: 매 청크 후 호출 → True 반환 시 InterruptedError.
        """
        media = MediaFileUpload(
            str(local_path),
            resumable=True,
            chunksize=chunk_size,
        )

        try:
            return self._upload_resumable_loop(
                media, local_path, parent_id, name, existing_id,
                progress_cb, bandwidth_limiter, total,
                chunk_size, stop_checker,
            )
        finally:
            # MediaFileUpload는 내부 _fd를 들고 있어 예외 시 핸들이 남는다.
            # Windows에서는 이후 rename/delete가 차단되므로 명시 해제.
            fd = getattr(media, "_fd", None)
            if fd is not None:
                try:
                    fd.close()
                except Exception:
                    pass

    def _upload_resumable_loop(
        self, media, local_path, parent_id, name, existing_id,
        progress_cb, bandwidth_limiter, total,
        chunk_size, stop_checker,
    ) -> DriveFile:
        if existing_id:
            request = self.service.files().update(
                fileId=existing_id,
                media_body=media,
                fields=FILE_FIELDS,
            )
        else:
            body = {"name": name, "parents": [parent_id]}
            request = self.service.files().create(
                body=body,
                media_body=media,
                fields=FILE_FIELDS,
            )

        response = None
        last_progress = 0
        # 단일 청크 모드(chunk_size=-1)인지 — 사전 토큰 차감량 결정에 사용
        single_chunk_mode = (chunk_size == -1)

        # 단일 청크 모드: PUT 시작 전에 파일 전체 양을 사전 consume.
        # 이렇게 해야 회사 회선이 풀스피드로 throttle 없이 터지는 일이 없음.
        # 1초 단위로 분할 sleep 하면서 progress_cb 갱신 → 사용자가 "멈춤"으로 오해 안 함.
        if single_chunk_mode and bandwidth_limiter and total > 0:
            log.info(
                f"⏸ 대역폭 제한 사전 대기 — {local_path.name} ({total/1024/1024:.1f} MB)"
            )

            def _throttle_cb(consumed, total_bytes):
                # progress_cb 와 같은 시그니처로 호출 → GUI 진행 상황 표시 갱신
                if progress_cb:
                    progress_cb(consumed, total_bytes)

            bandwidth_limiter.consume_upload_with_progress(
                total, callback=_throttle_cb, stop_checker=stop_checker,
            )

            # 중단 요청이면 PUT 자체를 안 함
            if stop_checker and stop_checker():
                raise InterruptedError("사용자 중단 요청")

        while response is None:
            # 중단 체크 (매 청크 사이)
            if stop_checker and stop_checker():
                raise InterruptedError("사용자 중단 요청")

            # ─ 일반 청크 모드 사전 대역폭 토큰 확보 ─
            # 청크 전송 전에 토큰 받아서 sleep → 그다음 청크 풀스피드 전송
            # 이렇게 하면 평균 ≈ 순간 속도. burst 시간 = 청크 크기 / 회선속도 로 제한됨.
            if bandwidth_limiter and not single_chunk_mode:
                next_chunk_bytes = min(chunk_size, total - last_progress)
                if next_chunk_bytes > 0:
                    bandwidth_limiter.consume_upload(next_chunk_bytes)
            # 단일 청크 모드는 위에서 이미 전체 사전 consume 했으므로 여기서는 추가 차감 안 함.

            try:
                status, response = request.next_chunk()
                current = status.resumable_progress if status else total
                if progress_cb and status:
                    progress_cb(current, total)
                last_progress = current
            except HttpError as e:
                status_code = getattr(e.resp, "status", 0)
                if _is_retryable(e, status_code):
                    time.sleep(2 + random.random())
                    continue
                raise
            # httplib2 308 버그는 여기서 잡지 않고 상위(_upload_resumable)로 전파
            # → wrapper가 단일 청크 모드로 재시도
        if progress_cb:
            progress_cb(total, total)
        return DriveFile.from_api(response)

    def _select_chunk_size(
        self,
        file_size: int,
        bandwidth_limiter=None,
    ) -> int:
        """파일 크기 + 대역폭 제한에 따른 최적 청크 크기.

        대역폭 제한이 있으면 '2초치 데이터' 크기를 기준으로 청크를 줄여
        중단 버튼 반응성을 개선한다 (느린 연결에서 큰 청크 = 긴 대기).
        최소 1MB, 최대는 크기 기반 기본값(8/32MB) 유지.
        """
        perf = self.performance
        if not perf.chunk_size_auto:
            return max(self.net.chunk_size, 256 * 1024)

        # 크기 기반 상한
        if file_size >= 100 * 1024 * 1024:
            size_limit = max(perf.chunk_size_large, 256 * 1024)
        else:
            size_limit = max(perf.chunk_size_medium, 256 * 1024)

        # 대역폭 제한이 있으면 2초치 데이터로 청크 크기 조절
        if bandwidth_limiter is not None:
            try:
                status = bandwidth_limiter.get_status()
                up_mbps = status.get("upload_mbps", 0)
                if up_mbps and not status.get("upload_unlimited", True):
                    bw_chunk = int(up_mbps * 2 * 1024 * 1024)  # 2초치 bytes
                    bw_chunk = max(bw_chunk, 1 * 1024 * 1024)   # 최소 1MB
                    return min(bw_chunk, size_limit)
            except Exception:
                pass

        return size_limit

    # ──────────────────────────────────────────────
    # 다운로드
    # ──────────────────────────────────────────────
    def download_file(
        self,
        file_id: str,
        dest_path: Path,
        progress_cb: Optional[Callable[[int, int], None]] = None,
        bandwidth_limiter: Optional[BandwidthLimiter] = None,
    ) -> None:
        """Drive 파일을 로컬로 다운로드 (resumable).

        작은 파일은 MediaIoBaseDownload 한 번 호출로 끝나므로 별도 simple 분기 불필요.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest_path.with_suffix(dest_path.suffix + ".gdrsync.part")

        # 파일 크기 사전 조회 (청크 크기 결정용)
        meta = self._retry(lambda: self.service.files().get(
            fileId=file_id, fields="size",
        ).execute())
        total_size = int(meta.get("size") or 0)
        chunk_size = self._select_chunk_size(total_size, bandwidth_limiter)

        # acknowledge_abuse=True 면 Drive가 차단한 '악성 의심' 파일도 시도
        # (Google Drive v3 API: get_media는 acknowledgeAbuse 파라미터 지원)
        if self.acknowledge_abuse:
            request = self.service.files().get_media(
                fileId=file_id, acknowledgeAbuse=True,
            )
        else:
            request = self.service.files().get_media(fileId=file_id)
        fh = io.FileIO(str(tmp), mode="wb")
        downloader = MediaIoBaseDownload(fh, request, chunksize=chunk_size)

        done = False
        last_progress = 0
        import os
        completed = False
        try:
            try:
                while not done:
                    # ─ 사전 대역폭 토큰 확보 ─
                    # 청크 받기 전에 토큰 받아서 sleep → 그다음 청크 풀스피드로 받음
                    if bandwidth_limiter and total_size > 0:
                        next_chunk_bytes = min(chunk_size, total_size - last_progress)
                        if next_chunk_bytes > 0:
                            bandwidth_limiter.consume_download(next_chunk_bytes)
                    try:
                        status, done = downloader.next_chunk()
                        current = int(status.resumable_progress) if status else total_size
                        if progress_cb and status:
                            progress_cb(current, int(status.total_size or total_size))
                        last_progress = current
                    except HttpError as e:
                        status_code = getattr(e.resp, "status", 0)
                        if _is_retryable(e, status_code):
                            time.sleep(2 + random.random())
                            continue
                        raise
            finally:
                fh.close()

            # 원자적 rename
            os.replace(tmp, dest_path)
            completed = True
        finally:
            # 실패/중단 시 .gdrsync.part 잔존 제거.
            # (성공 시엔 os.replace로 이미 사라져 있음)
            if not completed:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def search_by_name(
        self,
        name: str,
        *,
        exact: bool = False,
        include_trashed: bool = True,
        limit: int = 50,
    ) -> list[dict]:
        """이름으로 Drive 전역 검색 — 진단용.

        살아있는 항목 + 휴지통 항목 모두 반환. parents/owners/trashed 메타 포함.
        핑퐁/ghost-cleanup 의심 시 "이 폴더 Drive 어디 있나, trashed 인가" 즉시 확인.
        """
        escaped = _escape_q(name)
        if exact:
            q = f"name = '{escaped}'"
        else:
            q = f"name contains '{escaped}'"
        if not include_trashed:
            q += " and trashed = false"

        fields = (
            "nextPageToken, files(id, name, mimeType, trashed, parents, "
            "owners(emailAddress,displayName), modifiedTime, size, "
            "explicitlyTrashed, ownedByMe)"
        )
        results: list[dict] = []
        page_token = None
        while True:
            resp = self._retry(lambda: self.service.files().list(
                q=q,
                fields=fields,
                pageSize=min(100, limit - len(results)),
                pageToken=page_token,
                includeItemsFromAllDrives=False,
                supportsAllDrives=False,
            ).execute())
            results.extend(resp.get("files", []))
            if len(results) >= limit:
                break
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return results[:limit]

    def get_parent_path(self, file_id: str, max_depth: int = 10) -> str:
        """파일/폴더 id 의 부모 체인을 따라 올라가서 절대 경로 문자열 구성."""
        names: list[str] = []
        current = file_id
        for _ in range(max_depth):
            try:
                resp = self._retry(lambda: self.service.files().get(
                    fileId=current, fields="id, name, parents",
                ).execute())
            except Exception:
                break
            names.append(resp.get("name", "?"))
            parents = resp.get("parents") or []
            if not parents:
                break
            current = parents[0]
            if current == "root":
                names.append("My Drive")
                break
        return "/".join(reversed(names))

    def move_file(
        self,
        file_id: str,
        new_parent_id: Optional[str] = None,
        old_parent_id: Optional[str] = None,
        new_name: Optional[str] = None,
        keep_modified_time: str = "",
    ) -> DriveFile:
        """파일을 다른 폴더로 이동하거나 이름 변경 (서버측 메타 연산 — 재전송 없음).

        keep_modified_time: 원본 modifiedTime(RFC3339)을 그대로 유지해
        rename/이동이 '내용 변경'으로 오판되는 부작용을 방지 (빈 문자열이면 미지정).
        new_parent_id 가 old_parent_id 와 같으면 부모 변경은 생략 (이름만 변경).
        """
        body: dict = {}
        if new_name:
            body["name"] = new_name
        if keep_modified_time:
            body["modifiedTime"] = keep_modified_time
        kwargs: dict = dict(fileId=file_id, body=body, fields=FILE_FIELDS)
        if new_parent_id and new_parent_id != old_parent_id:
            kwargs["addParents"] = new_parent_id
            if old_parent_id:
                kwargs["removeParents"] = old_parent_id
        resp = self._retry(lambda: self.service.files().update(**kwargs).execute())
        return DriveFile.from_api(resp)

    def folder_has_children(self, folder_id: str) -> bool:
        """폴더에 살아있는(trash 아님) 자식이 하나라도 있는지 — pageSize=1 로 저렴하게."""
        resp = self._retry(lambda: self.service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="files(id)",
            pageSize=1,
            spaces="drive",
            supportsAllDrives=False,
        ).execute())
        return bool(resp.get("files"))

    def invalidate_cached_path(self, remote_path: str) -> None:
        """path_cache 에서 해당 경로와 그 하위 경로를 제거.

        폴더를 trash 한 뒤 캐시가 죽은 id 를 계속 돌려주면 이후 업로드가
        휴지통 폴더 안으로 들어가므로 반드시 무효화해야 한다.
        """
        remote_path = (remote_path or "").strip("/")
        if not remote_path:
            return
        with self._path_cache_lock:
            stale = [
                k for k in self._path_cache
                if k == remote_path or k.startswith(remote_path + "/")
            ]
            for k in stale:
                del self._path_cache[k]

    def delete_file(self, file_id: str, permanent: bool = False) -> None:
        """파일을 휴지통으로 보내거나 영구 삭제."""
        if permanent:
            self._retry(lambda: self.service.files().delete(fileId=file_id).execute())
        else:
            self._retry(lambda: self.service.files().update(
                fileId=file_id,
                body={"trashed": True},
            ).execute())

    def get_file(self, file_id: str) -> DriveFile:
        resp = self._retry(lambda: self.service.files().get(
            fileId=file_id,
            fields=FILE_FIELDS,
        ).execute())
        return DriveFile.from_api(resp)

    # 외부(TransferPool)에서 credentials 재사용하기 위한 접근자
    @property
    def credentials(self):
        return self._credentials

    @property
    def shared_path_cache(self) -> tuple[dict, threading.Lock]:
        return self._path_cache, self._path_cache_lock


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def _escape_q(s: str) -> str:
    """Drive API 쿼리 문자열 이스케이프."""
    return s.replace("\\", "\\\\").replace("'", "\\'")
