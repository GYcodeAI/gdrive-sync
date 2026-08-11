"""Google OAuth 2.0 데스크톱 앱 인증 모듈.

- credentials.json → token.json 생성 (최초 1회)
- refresh token 자동 갱신
- 포트 자동 탐색 (run_local_server)
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow


log = logging.getLogger(__name__)

# 전체 Drive 접근 권한 (읽기/쓰기/삭제)
SCOPES = ["https://www.googleapis.com/auth/drive"]

DEFAULT_CREDENTIALS_PATH = Path("credentials.json")
# pip 설치 배포용 고정 위치 — CWD 에 credentials.json 이 없으면 여기서 찾음
USER_CREDENTIALS_PATH = Path.home() / ".gdrive_sync" / "credentials.json"
DEFAULT_TOKEN_PATH = Path.home() / ".gdrive_sync" / "token.json"


def resolve_credentials_path(path: Path = DEFAULT_CREDENTIALS_PATH) -> Path:
    """credentials.json 탐색: 지정/CWD 경로 → ~/.gdrive_sync/credentials.json 폴백.

    실행 위치에 무관하게 동작해야 하는 pip 설치 사용자를 위해, 기본 경로에
    파일이 없으면 사용자 홈의 고정 위치를 대신 사용한다. 둘 다 없으면
    원래 경로를 그대로 반환 (에러 메시지가 기본 경로 기준으로 나가도록).
    """
    p = Path(path)
    if p.exists():
        return p
    if USER_CREDENTIALS_PATH.exists():
        return USER_CREDENTIALS_PATH
    return p


class AuthError(RuntimeError):
    pass


def humanize_oauth_error(err: Exception) -> str:
    """Google OAuth 예외 메시지를 한글 안내문 + 해결책으로 변환.

    Google이 반환하는 영어 raw error를 코딩 비전문가도 알아볼 수 있는
    한글 메시지로 매핑. 알려진 패턴이 없으면 원문을 그대로 포함.
    """
    msg = str(err)
    # 키워드 기반 매핑 (가장 흔한 순)
    table = [
        (
            "invalid_grant",
            "Google 토큰이 만료되거나 철회됐습니다.\n"
            "  → 해결: 터미널에서 'gdrive-sync auth' 실행해 재인증하세요.",
        ),
        (
            "Token has been expired or revoked",
            "토큰이 만료/철회된 상태입니다.\n"
            "  → 해결: 'gdrive-sync auth'로 재인증하세요.",
        ),
        (
            "invalid_client",
            "OAuth 클라이언트 정보가 잘못됐습니다 (credentials.json).\n"
            "  → 해결: Google Cloud Console에서 발급한 OAuth 클라이언트 ID·secret이\n"
            "          credentials.json과 일치하는지 확인하세요.",
        ),
        (
            "access_denied",
            "사용자가 권한 부여를 거부했습니다.\n"
            "  → 해결: 'gdrive-sync auth'로 다시 시도하고 모든 권한을 승인하세요.",
        ),
        (
            "redirect_uri_mismatch",
            "OAuth redirect URI가 Google Cloud Console 설정과 다릅니다.\n"
            "  → 해결: 'OAuth 동의 화면 → 클라이언트' 의 redirect URI 확인.",
        ),
        (
            "invalid_scope",
            "요청 권한 범위(scope)가 Google Cloud Console에 등록 안 됨.\n"
            "  → 해결: OAuth 동의 화면에서 Drive 권한이 활성화되어 있는지 확인.",
        ),
        (
            "rate_limit",
            "API 호출이 짧은 시간 내 너무 많아 일시 제한됨.\n"
            "  → 해결: 몇 분 기다렸다가 다시 시도하세요.",
        ),
        (
            "quotaExceeded",
            "Drive API 사용 한도 초과 (Google 일일 쿼터).\n"
            "  → 해결: 24시간 후 자동 리셋. 큰 작업이면 분산 실행 검토.",
        ),
    ]
    for keyword, friendly in table:
        if keyword.lower() in msg.lower():
            return f"{friendly}\n  (원문: {msg[:200]})"
    # 네트워크/연결 계열
    lower = msg.lower()
    if any(k in lower for k in ("connection", "timeout", "timed out", "name resolution")):
        return (
            "네트워크 연결 문제로 Google 서버에 접속하지 못했습니다.\n"
            "  → 해결: 인터넷 연결, 회사 방화벽/프록시 상태 확인.\n"
            f"  (원문: {msg[:200]})"
        )
    # 매핑 없는 에러는 원문 그대로 (영어지만 적어도 보존)
    return f"인증 처리 중 알 수 없는 오류: {msg}"


def load_credentials(
    credentials_path: Path = DEFAULT_CREDENTIALS_PATH,
    token_path: Path = DEFAULT_TOKEN_PATH,
    interactive: bool = False,
) -> Credentials:
    """유효한 Credentials 객체 반환.

    - token.json이 있으면 먼저 시도 → 만료 시 refresh
    - refresh 실패 또는 token 없음 → interactive=True면 브라우저 플로우, 아니면 에러
    """
    token_path.parent.mkdir(parents=True, exist_ok=True)

    creds: Optional[Credentials] = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except ValueError as e:
            log.warning(f"token.json 읽기 실패: {e}")

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds, token_path)
            log.info("액세스 토큰 자동 갱신 완료")
            return creds
        except Exception as e:
            # 한글 원인 + 해결책으로 풀어서 알림 (사용자 친화)
            log.warning(f"토큰 갱신 실패:\n  {humanize_oauth_error(e)}")
            creds = None

    # 인터랙티브 플로우 필요
    if not interactive:
        raise AuthError(
            "유효한 토큰이 없습니다. 먼저 'gdrive-sync auth' 명령을 실행하세요."
        )

    credentials_path = resolve_credentials_path(credentials_path)
    if not credentials_path.exists():
        raise AuthError(
            f"'{credentials_path}' 파일이 없습니다.\n\n"
            f"Google Cloud Console에서 OAuth 2.0 클라이언트 ID(데스크톱 앱)를\n"
            f"발급받고 JSON 파일을 '{credentials_path}' 또는\n"
            f"'{USER_CREDENTIALS_PATH}' 로 저장하세요.\n\n"
            f"설정 가이드:\n"
            f"  https://developers.google.com/workspace/guides/create-credentials\n"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    port = _find_free_port()
    log.info(f"브라우저에서 로그인을 진행하세요 (로컬 포트 {port})...")
    creds = flow.run_local_server(
        port=port,
        prompt="consent",          # refresh_token 강제 발급
        access_type="offline",
        open_browser=True,
    )
    _save_token(creds, token_path)
    log.info(f"인증 완료. 토큰이 {token_path}에 저장되었습니다.")
    return creds


def _save_token(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())
    # 퍼미션 축소 (POSIX만)
    try:
        token_path.chmod(0o600)
    except OSError:
        pass


def _find_free_port() -> int:
    """OS에게 사용 가능한 포트 요청."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def revoke_token(token_path: Path = DEFAULT_TOKEN_PATH) -> bool:
    """token.json 삭제."""
    if token_path.exists():
        token_path.unlink()
        return True
    return False
