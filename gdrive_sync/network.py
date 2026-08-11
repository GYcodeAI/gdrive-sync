"""네트워크 연결 모듈.

- httplib2.Http 객체 생성 (직접 연결 / HTTP 프록시 / SOCKS 프록시)
- 시스템 프록시 자동 감지 (환경변수, Windows 레지스트리)
- test-connection 명령용 연결 진단
"""

from __future__ import annotations

import logging
import os
import socket
import ssl
import sys
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httplib2

try:
    import socks  # PySocks
    _HAS_SOCKS = True
except ImportError:  # pragma: no cover
    _HAS_SOCKS = False

from gdrive_sync.config import NetworkConfig


log = logging.getLogger(__name__)

# 구글 API 엔드포인트 (연결 테스트용)
GOOGLE_HOSTS = [
    ("www.googleapis.com", 443),
    ("accounts.google.com", 443),
    ("oauth2.googleapis.com", 443),
]


# ──────────────────────────────────────────────────────────
# 연결 테스트용 결과 구조
# ──────────────────────────────────────────────────────────

@dataclass
class ConnectionResult:
    method: str                 # "direct" | "system_proxy" | "http_proxy" | "socks"
    ok: bool
    detail: str
    elapsed_ms: int = 0


# ──────────────────────────────────────────────────────────
# 시스템 프록시 감지
# ──────────────────────────────────────────────────────────

def detect_system_proxy() -> Optional[tuple[str, int]]:
    """환경변수 / OS 설정에서 HTTPS 프록시를 감지.

    반환: (host, port) 또는 None.
    """
    # 1) 환경변수 우선 (urllib는 https_proxy / HTTPS_PROXY / http_proxy 순서로 확인)
    proxies = urllib.request.getproxies()
    https_url = proxies.get("https") or proxies.get("http")
    if https_url:
        parsed = urlparse(https_url if "://" in https_url else "http://" + https_url)
        if parsed.hostname and parsed.port:
            return parsed.hostname, parsed.port

    # 2) Windows 레지스트리 fallback
    if sys.platform == "win32":
        try:
            import winreg  # type: ignore
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
            )
            enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            if enabled:
                server, _ = winreg.QueryValueEx(key, "ProxyServer")
                winreg.CloseKey(key)
                # "host:port" 혹은 "http=host:port;https=host:port"
                for part in str(server).split(";"):
                    if "=" in part:
                        scheme, hp = part.split("=", 1)
                        if scheme.strip().lower() == "https":
                            h, p = hp.split(":")
                            return h, int(p)
                    elif ":" in part:
                        h, p = part.split(":")
                        return h, int(p)
        except (OSError, FileNotFoundError, ValueError):
            pass

    return None


# ──────────────────────────────────────────────────────────
# httplib2.Http 팩토리
# ──────────────────────────────────────────────────────────

def build_http(net: NetworkConfig) -> httplib2.Http:
    """NetworkConfig에 따른 httplib2.Http 인스턴스 생성.

    google-auth-httplib2의 AuthorizedHttp가 이 객체를 감쌈.
    """
    proxy_info = _resolve_proxy_info(net)

    http = httplib2.Http(
        timeout=net.timeout,
        proxy_info=proxy_info,
        disable_ssl_certificate_validation=False,
    )
    return http


def _resolve_proxy_info(net: NetworkConfig) -> Optional[httplib2.ProxyInfo]:
    """NetworkConfig → httplib2.ProxyInfo."""
    # 1) 명시적 프록시 사용
    if net.use_proxy and net.proxy_host:
        proxy_type = _httplib2_proxy_type(net.proxy_type)
        return httplib2.ProxyInfo(
            proxy_type=proxy_type,
            proxy_host=net.proxy_host,
            proxy_port=net.proxy_port,
            proxy_user=net.proxy_username or None,
            proxy_pass=net.proxy_password or None,
        )

    # 2) 시스템 프록시 자동 감지
    if net.use_system_proxy:
        sys_proxy = detect_system_proxy()
        if sys_proxy:
            host, port = sys_proxy
            log.info(f"시스템 프록시 감지: {host}:{port}")
            return httplib2.ProxyInfo(
                proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
                proxy_host=host,
                proxy_port=port,
            )

    # 3) 직접 연결
    return None


def _httplib2_proxy_type(name: str) -> int:
    name = (name or "http").lower()
    if name == "http":
        return httplib2.socks.PROXY_TYPE_HTTP
    if name == "socks4":
        return httplib2.socks.PROXY_TYPE_SOCKS4
    if name == "socks5":
        return httplib2.socks.PROXY_TYPE_SOCKS5
    return httplib2.socks.PROXY_TYPE_HTTP


# ──────────────────────────────────────────────────────────
# 연결 테스트
# ──────────────────────────────────────────────────────────

def test_connections(net: NetworkConfig) -> list[ConnectionResult]:
    """각 연결 전략별로 결과를 수집."""
    import time
    results: list[ConnectionResult] = []

    # 1) 직접 TCP 연결 테스트
    results.append(_test_tcp_direct())

    # 2) TLS 핸드셰이크까지
    results.append(_test_tls_direct())

    # 3) 시스템 프록시 감지 결과
    sys_proxy = detect_system_proxy()
    if sys_proxy:
        results.append(
            ConnectionResult(
                method="system_proxy",
                ok=True,
                detail=f"{sys_proxy[0]}:{sys_proxy[1]} 감지됨",
            )
        )
    else:
        results.append(
            ConnectionResult(
                method="system_proxy",
                ok=False,
                detail="시스템 프록시 없음",
            )
        )

    # 4) 설정된 프록시로 실제 HTTP 요청
    if net.use_proxy and net.proxy_host:
        start = time.time()
        try:
            http = build_http(net)
            resp, _ = http.request("https://www.googleapis.com/generate_204", "GET")
            elapsed = int((time.time() - start) * 1000)
            ok = resp.status in (204, 200)
            results.append(
                ConnectionResult(
                    method=f"configured_proxy ({net.proxy_type}://{net.proxy_host}:{net.proxy_port})",
                    ok=ok,
                    detail=f"HTTP {resp.status}",
                    elapsed_ms=elapsed,
                )
            )
        except Exception as e:
            results.append(
                ConnectionResult(
                    method=f"configured_proxy ({net.proxy_type}://{net.proxy_host}:{net.proxy_port})",
                    ok=False,
                    detail=f"실패: {e}",
                )
            )

    return results


def _test_tcp_direct() -> ConnectionResult:
    """TCP 수준 직접 연결 테스트 (443)."""
    import time
    start = time.time()
    try:
        host, port = GOOGLE_HOSTS[0]
        with socket.create_connection((host, port), timeout=10):
            pass
        return ConnectionResult(
            method="direct_tcp",
            ok=True,
            detail=f"{host}:{port} 연결 성공",
            elapsed_ms=int((time.time() - start) * 1000),
        )
    except Exception as e:
        return ConnectionResult(
            method="direct_tcp",
            ok=False,
            detail=f"실패: {e}",
        )


def _test_tls_direct() -> ConnectionResult:
    """TLS 핸드셰이크까지 포함한 직접 연결 테스트."""
    import time
    start = time.time()
    try:
        host, port = GOOGLE_HOSTS[0]
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                _ = ssock.version()
        return ConnectionResult(
            method="direct_tls",
            ok=True,
            detail=f"{host}:{port} TLS 성공",
            elapsed_ms=int((time.time() - start) * 1000),
        )
    except Exception as e:
        return ConnectionResult(
            method="direct_tls",
            ok=False,
            detail=f"실패: {e}",
        )
