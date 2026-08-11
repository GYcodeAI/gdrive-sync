"""동기화 중 시스템 sleep 방지.

설계 원칙: **시스템 절전만 막고, 디스플레이/화면잠금/화면보호기는 OS 정책에 맡긴다.**
야간 무인 동기화 시 사용자가 모니터 잠그고 자리를 비울 수 있어야 함.

macOS: caffeinate -ims -w <pid> (-d/-u 제외 → 디스플레이 절전·화면 잠금 허용;
       -w 로 부모 프로세스와 수명을 묶어 고아 프로세스 누수 방지)
Linux: systemd-inhibit --what=sleep:handle-lid-switch (idle 제외 → 화면보호기 동작)
Windows: SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
         ES_AWAYMODE_REQUIRED는 디스플레이를 즉시 끄는 부작용이 있어 제거 →
         일반 전원 정책대로 화면보호기/잠금이 정상 동작

사용법:
    inhibitor = SleepInhibitor()
    inhibitor.start()
    try:
        ...작업...
    finally:
        inhibitor.stop()

또는 컨텍스트 매니저:
    with SleepInhibitor():
        ...작업...

Amphetamine 연동 (macOS 전용):
    mac_lid_is_closed()          → 뚜껑 닫힘 여부
    amphetamine_start_session()  → 무기한 세션 시작 (동기화 시작 시)
    amphetamine_end_session()    → 세션 종료 (동기화 완료 시)

    ※ Amphetamine 트리거("앱 실행 중")는 제거하고 GUI가 직접 세션을 제어하는 방식을
       권장합니다. 그래야 동기화 완료 후 뚜껑을 닫으면 정상적으로 절전됩니다.
"""

from __future__ import annotations

import atexit
import ctypes
import logging
import os
import subprocess
import sys
import threading


log = logging.getLogger(__name__)


# Windows SetThreadExecutionState flags
# https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate
_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
# ES_AWAYMODE_REQUIRED는 의도적으로 사용하지 않음:
# Media Center 용 플래그로, 활성화하면 디스플레이가 "절전된 것처럼" 즉시 꺼져
# 화면보호기/잠금 정책을 우회한다. 야간 무인 동기화 시 화면 잠금이 안 걸리는 원인.


class SleepInhibitor:
    """OS별 sleep 방지 헬퍼.

    - macOS: `caffeinate -ims -w <pid>` (-i idle / -m 디스크 / -s 시스템(AC)).
             -d(디스플레이)·-u(사용자 활동)는 제외 → 자리 비움 시 화면 잠금 정상 동작.
             -w <pid> → 부모(앱) 프로세스가 죽으면 caffeinate도 자동 종료(고아 방지).
    - Linux: `systemd-inhibit --what=sleep:handle-lid-switch sleep infinity`.
             idle 제외 → 화면보호기/스크린락 정상 동작. systemd 미설치 환경에선 패스.
    - Windows: kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED).
               AWAYMODE 미사용 → 화면보호기/잠금 정상 동작, 시스템 sleep만 차단.
               노트북 뚜껑 닫기는 막을 수 없음(전원 정책 영역).

    안전 장치:
    - 시작 실패 시 조용히 패스 (FileNotFoundError 등) → 동기화 자체엔 영향 없음
    - 강제 종료 시에도 atexit으로 자식 프로세스 정리
    - 다중 start() 호출은 idempotent (이미 실행 중이면 무시)
    """

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._win_active: bool = False
        self._lock = threading.Lock()
        self._atexit_registered = False

    # ──────────────────────────────────────────────
    # 공개 API
    # ──────────────────────────────────────────────

    def start(self) -> bool:
        """Sleep 방지 시작. 성공/이미 실행 중 시 True, OS 미지원/실행 실패 시 False."""
        with self._lock:
            # Windows: ctypes 경로 (서브프로세스 없음)
            if sys.platform == "win32":
                if self._win_active:
                    return True
                ok = self._win_set_state(
                    _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
                )
                if not ok:
                    return False
                self._win_active = True
                if not self._atexit_registered:
                    atexit.register(self._atexit_cleanup)
                    self._atexit_registered = True
                log.info("💤 시스템 sleep 방지 활성화 (SetThreadExecutionState)")
                return True

            # macOS / Linux: 서브프로세스 경로
            if self._proc is not None and self._proc.poll() is None:
                return True   # 이미 실행 중

            # 과거 버전(-dimsu)이 남겼을 수 있는 고아 caffeinate 정리 (macOS, best-effort)
            self._reap_legacy_orphans()

            cmd = self._build_command()
            if cmd is None:
                log.debug(f"이 OS({sys.platform})에서는 sleep 방지 미지원 — 패스")
                return False

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                log.debug(f"sleep 방지 명령어 없음 ({cmd[0]}) — 패스")
                self._proc = None
                return False
            except Exception as e:
                log.warning(f"sleep 방지 시작 실패: {e}")
                self._proc = None
                return False

            # 강제 종료 시에도 자식 정리 보장
            if not self._atexit_registered:
                atexit.register(self._atexit_cleanup)
                self._atexit_registered = True

            log.info(f"💤 시스템 sleep 방지 활성화 ({' '.join(cmd)})")
            return True

    def stop(self) -> None:
        """Sleep 방지 종료. 멱등 — 안 켜져있어도 안전."""
        with self._lock:
            if sys.platform == "win32":
                if not self._win_active:
                    return
                self._win_set_state(_ES_CONTINUOUS)   # 플래그 해제
                self._win_active = False
                log.info("💤 시스템 sleep 방지 해제")
                return

            if self._proc is None:
                return
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=1)
            except Exception as e:
                log.debug(f"sleep 방지 종료 중 무시 가능 오류: {e}")
            finally:
                self._proc = None
                log.info("💤 시스템 sleep 방지 해제")

    def is_active(self) -> bool:
        """현재 sleep 방지가 활성 상태인지."""
        with self._lock:
            if sys.platform == "win32":
                return self._win_active
            return self._proc is not None and self._proc.poll() is None

    @staticmethod
    def _win_set_state(flags: int) -> bool:
        """Windows kernel32.SetThreadExecutionState 호출. 실패 시 False."""
        try:
            # 반환값 0 = 실패 (이전 상태가 0인 경우는 거의 없음 — 0 이면 에러로 간주)
            prev = ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
            if prev == 0:
                log.warning("SetThreadExecutionState 실패 (반환 0)")
                return False
            return True
        except Exception as e:
            log.warning(f"SetThreadExecutionState 호출 오류: {e}")
            return False

    # ──────────────────────────────────────────────
    # 컨텍스트 매니저 지원
    # ──────────────────────────────────────────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ──────────────────────────────────────────────
    # 내부
    # ──────────────────────────────────────────────

    def _build_command(self) -> list[str] | None:
        """현재 OS에 맞는 sleep 방지 명령어. 미지원이면 None."""
        if sys.platform == "darwin":
            # -i idle / -m 디스크 / -s 시스템(AC).
            # -d(디스플레이), -u(사용자 활동 시뮬)는 의도적으로 제외 → 화면 잠금 허용.
            # -w <pid>: 이 파이썬 프로세스가 종료되면 caffeinate도 자동으로 함께 종료.
            #   atexit/stop()이 호출되지 못하는 강제 종료(SIGKILL)·크래시·전원 차단
            #   상황에서도 고아(orphan) caffeinate가 남아 화면보호기를 막는 일을 차단.
            return ["caffeinate", "-i", "-m", "-s", "-w", str(os.getpid())]
        if sys.platform.startswith("linux"):
            # systemd-inhibit이 있으면 사용. idle 제외 → 화면보호기/스크린락 허용.
            return [
                "systemd-inhibit",
                "--what=sleep:handle-lid-switch",
                "--why=gdrive-sync 동기화 중",
                "--mode=block",
                "sleep", "infinity",
            ]
        # Windows 등
        return None

    def _atexit_cleanup(self) -> None:
        """프로세스 종료 시 자식 정리."""
        try:
            self.stop()
        except Exception:
            pass

    @staticmethod
    def _reap_legacy_orphans() -> None:
        """과거 버전이 남긴 고아 caffeinate 정리 (macOS 전용, best-effort).

        이 앱의 예전 버전은 `caffeinate -dimsu`(디스플레이까지 차단)를 띄웠는데,
        강제 종료 시 정리되지 못하고 launchd(PID 1)에 입양된 고아로 남아
        화면보호기를 영구히 막는 사례가 있었다. 현재 버전은 -dimsu를 전혀 쓰지
        않으므로, 시스템에 떠 있는 `caffeinate -dimsu`는 전부 과거 잔재로 보고 정리.
        (현재 버전이 쓰는 `-w` 방식은 부모와 함께 죽으므로 여기서 매칭되지 않음.)
        """
        if sys.platform != "darwin":
            return
        try:
            result = subprocess.run(
                ["pkill", "-f", "caffeinate -dimsu"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if result.returncode == 0:
                log.info("🧹 과거 고아 caffeinate(-dimsu) 정리 완료")
        except Exception as e:
            log.debug(f"고아 caffeinate 정리 생략: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# macOS 전용 유틸리티
# ──────────────────────────────────────────────────────────────────────────────

def mac_lid_is_closed() -> bool:
    """macOS에서 맥북 뚜껑(클램쉘) 닫힘 여부를 반환.

    `ioreg`로 AppleClamshellState 레지스터를 읽습니다.
    - True  → 뚜껑 닫힘
    - False → 뚜껑 열림 또는 macOS 외 플랫폼 / 오류

    데스크톱 Mac(뚜껑 없음)은 항상 False.
    """
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(
            ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
            capture_output=True, text=True, timeout=3,
        )
        return "AppleClamshellState = Yes" in result.stdout
    except Exception as e:
        log.debug(f"mac_lid_is_closed 조회 실패: {e}")
        return False


def amphetamine_start_session() -> bool:
    """Amphetamine 앱에 무기한 세션 시작을 AppleScript로 요청.

    Amphetamine이 설치되어 있지 않거나 실행 중이 아니면 조용히 False 반환.
    반환값: True = 성공, False = 실패/미설치
    """
    if sys.platform != "darwin":
        return False
    script = (
        'tell application "Amphetamine" to start new session'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            log.info("☕ Amphetamine 세션 시작 (동기화 중 절전 방지)")
            return True
        else:
            log.debug(f"Amphetamine 세션 시작 실패: {result.stderr.strip()}")
            return False
    except Exception as e:
        log.debug(f"Amphetamine 세션 시작 오류: {e}")
        return False


def amphetamine_end_session() -> bool:
    """Amphetamine 앱의 현재 세션을 AppleScript로 종료.

    세션이 없거나 Amphetamine이 실행 중이 아니면 조용히 False 반환.
    반환값: True = 성공, False = 실패/세션 없음/미설치
    """
    if sys.platform != "darwin":
        return False
    script = 'tell application "Amphetamine" to end session'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            log.info("☕ Amphetamine 세션 종료 (절전 복원)")
            return True
        else:
            log.debug(f"Amphetamine 세션 종료 실패: {result.stderr.strip()}")
            return False
    except Exception as e:
        log.debug(f"Amphetamine 세션 종료 오류: {e}")
        return False


def amphetamine_is_active() -> bool:
    """Amphetamine 세션이 현재 활성 상태인지 확인."""
    if sys.platform != "darwin":
        return False
    script = 'tell application "Amphetamine" to return session is active'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0 and result.stdout.strip().lower() == "true"
    except Exception:
        return False


def terminate_launcher_applet() -> bool:
    """GUI를 띄운 launch-gui.app 런처(AppleScript applet)를 종료 (macOS 전용).

    ★ 배터리 방전 사고의 핵심 원인 정리용 ★
    launch-gui.app 은 `do shell script "... &"` 로 파이썬 GUI를 띄우는데,
    이 applet 프로세스가 GUI 수명 내내 살아남아 macOS의
    `PreventUserIdleSystemSleep` 전력 어서션("handling Apple event")을
    계속 쥐고 있다. 그 결과 GUI가 떠 있는 동안 맥북이 (뚜껑을 닫아도)
    절전에 들어가지 못해 밤새 배터리가 소모된다.

    파이썬 GUI 프로세스와 applet 은 부모-자식 관계가 아니라(둘 다 launchd
    입양, PPID 1) 서로 독립적이라, 파이썬만 종료해선 applet 어서션이 안 풀린다.
    따라서 "프로그램 종료" 시 이 applet 도 함께 정리해 어서션을 반드시 해제한다.

    반환값: True = 종료 신호 보냄(또는 대상 없음), False = 오류.
    """
    if sys.platform != "darwin":
        return False
    try:
        # 본 프로젝트의 런처 경로 패턴만 정확히 매칭 (다른 applet 오살상 방지)
        subprocess.run(
            ["pkill", "-f", "launch-gui.app/Contents/MacOS/applet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=3,
        )
        log.info("🛑 GUI 런처(applet) 종료 — sleep 차단 어서션 해제")
        return True
    except Exception as e:
        log.debug(f"런처 applet 종료 생략: {e}")
        return False
