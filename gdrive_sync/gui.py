"""gdrive-sync 간단 GUI (Tkinter 기반).

Python 내장 Tkinter를 사용해 추가 의존성 없이 동작.
Windows/macOS 네이티브 룩 & 한글 완벽 지원.

사용법:
    python -m gdrive_sync gui
    또는 (Windows) launch-gui.bat 더블클릭
    또는 (macOS)   launch-gui.command 더블클릭

기능:
- 상태 패널: 동기화 폴더 / 마지막 실행 / 활성 대역폭 규칙 / 인증 상태
- 동기화 시작 / 미리보기 / 연결 테스트 / 중단
- 빠른 설정: 대역폭 on/off, 병렬 전송 수 슬라이더
- 실시간 로그 (색상 구분: INFO/WARN/ERROR)
- 메뉴: 인증 / 설정 편집 / 상태 초기화 / 정보
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from gdrive_sync import __version__
from gdrive_sync.auth import (
    AuthError, DEFAULT_TOKEN_PATH, humanize_oauth_error, load_credentials,
)
from gdrive_sync.bandwidth import make_limiter
from gdrive_sync.eta import ETACalculator
from gdrive_sync.progress import ProgressTracker
from gdrive_sync.sleep_inhibitor import (
    SleepInhibitor,
    mac_lid_is_closed,
    amphetamine_start_session,
    amphetamine_end_session,
    terminate_launcher_applet,
)


def _ensure_viewable(win) -> None:
    """모달 grab_set 직전, 부모 창이 최소화/숨김이면 강제로 복원한다.

    ⚠ 핵심 버그 방지: 부모 창이 iconic(최소화)/withdrawn 인 상태에서 그 자식
    Toplevel 이 transient + grab_set 으로 떠 버리면, 모달 자식도 화면에 안
    보이는 채로 입력 grab 을 잡는다. 사용자는 메인 창을 작업표시줄에서 복원해도
    보이지 않는 모달이 입력을 가로채 창이 "안 나오는" 것처럼 멈춘다.
    (동기화 완료 → HistoryDialog / 미리보기 완료 → PreviewResultDialog 자동 표시
    시점에 사용자가 창을 최소화해 둔 경우 재현.)

    grab 을 걸기 전에 부모를 deiconify + lift 해서 항상 보이는 상태로 만든다.
    """
    try:
        st = win.state()
    except Exception:
        return
    if st in ("iconic", "withdrawn"):
        try:
            win.deiconify()
            win.lift()
            win.update_idletasks()
        except Exception:
            pass


def _bind_tooltip(widget, text: str) -> None:
    """간단한 툴팁 — 마우스 오버 시 작은 라벨 표시."""
    tip = {"win": None}

    def show(_evt=None):
        if tip["win"] is not None:
            return
        try:
            x = widget.winfo_rootx() + 10
            y = widget.winfo_rooty() + widget.winfo_height() + 4
            t = tk.Toplevel(widget)
            t.wm_overrideredirect(True)
            t.wm_geometry(f"+{x}+{y}")
            tk.Label(
                t, text=text, justify="left",
                background="#FFFCE6", foreground="#333",
                relief="solid", borderwidth=1,
                font=("Segoe UI", 9) if sys.platform == "win32" else ("Helvetica", 11),
                padx=6, pady=4,
            ).pack()
            tip["win"] = t
        except Exception:
            pass

    def hide(_evt=None):
        if tip["win"] is not None:
            try:
                tip["win"].destroy()
            except Exception:
                pass
            tip["win"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<ButtonPress>", hide, add="+")
from gdrive_sync.config import (
    DEFAULT_CONFIG_PATH, BandwidthSchedule, SchedulerJob,
    load_config, save_config, default_config_template,
)
from gdrive_sync.history import SyncRecord, append_record, load_history, get_stats, format_timestamp_local
from gdrive_sync.notify import notify, notify_sync_complete
from gdrive_sync.state import clear_state, load_state


log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────
# 시스템 컬러 팔레트 (Apple HIG Light — iOS/macOS)
# 기존 키 이름을 유지하여 하위 호환 보장 (값만 교체됨).
# ──────────────────────────────────────────────────────────

CLAUDE_COLORS = {
    # 서피스 (시스템 배경)
    "bg_primary":   "#FFFFFF",    # systemBackground — 메인 창
    "bg_secondary": "#F2F2F7",    # systemGroupedBackground — 그룹 배경
    "bg_elevated":  "#FFFFFF",    # 카드/팝업 (흰색, 그림자로 구분)
    "bg_input":     "#FFFFFF",    # 입력 필드

    # 액센트 (systemBlue — 단일 액센트)
    "accent":       "#007AFF",    # systemBlue
    "accent_hover": "#0062CC",    # 호버 (약간 어둡게)
    "accent_light": "#E8F1FF",    # 선택/포커스 링 배경

    # 텍스트 (Apple label colors)
    "text_primary":   "#000000",  # label
    "text_secondary": "#3C3C43",  # secondaryLabel
    "text_muted":     "#8E8E93",  # tertiaryLabel ≈ systemGray
    "text_inverse":   "#FFFFFF",  # 다크 위 텍스트

    # 테두리 (separator)
    "border":         "#D1D1D6",  # systemGray4
    "border_focus":   "#007AFF",  # 포커스 = systemBlue

    # 상태 (iOS 시스템 컬러)
    "success":        "#34C759",  # systemGreen
    "warning":        "#FF9F0A",  # systemOrange
    "error":          "#FF3B30",  # systemRed
    "info":           "#007AFF",  # systemBlue

    # 로그 패널 (라이트 콘솔 — #F9F9FB 위, 짙은 시스템 컬러)
    "log_bg":         "#F9F9FB",
    "log_fg":         "#1D1D1F",
    "log_debug":      "#8E8E93",  # systemGray
    "log_info":       "#3C3C43",  # 라벨 보조
    "log_warning":    "#B25000",  # 라이트 배경용 짙은 오렌지
    "log_error":      "#C9241C",  # 라이트 배경용 짙은 레드
    "log_success":    "#0E8B3D",  # 라이트 배경용 짙은 그린
    "log_header":     "#0051D5",  # 라이트 배경용 짙은 블루
}


# ──────────────────────────────────────────────────────────
# 로그 → Queue 핸들러 (워커 스레드에서 UI 스레드로 안전하게 전달)
# ──────────────────────────────────────────────────────────

class QueueLogHandler(logging.Handler):
    """logging 레코드를 Queue에 넣어 UI 스레드에서 꺼내 쓰게 함."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.q.put(("log", record.levelname, msg))
        except Exception:
            pass


# ──────────────────────────────────────────────────────────
# 메인 애플리케이션
# ──────────────────────────────────────────────────────────

class SyncApp:
    """gdrive-sync Tkinter GUI."""

    REFRESH_INTERVAL_MS = 15_000        # 상태 자동 갱신 (대역폭 규칙 전환 감지)
    QUEUE_POLL_MS = 100                 # 로그 큐 폴링 주기

    # 무진행 워치독: 동기화가 busy 상태인 동안 항상 평가됨(체크박스 무관하게
    # 정지 감지 시 워커를 강제 종료). "동기화 완료 후 PC 종료"가 켜져 있을
    # 때만 그 뒤에 실제 PC 종료까지 이어짐 — _watchdog_finalize 참조.
    # 두 가지 경로로 평가:
    #   (A) 큐 메시지(log/progress/status)가 N초간 전혀 없으면 워커 정지 간주.
    #   (B) 바이트 진척이 N초간 임계량 미만이면 "트리클" 간주.
    # (A) 단독으로는 속도가 2 B/s까지 떨어져도 가끔 청크 완료 메시지가 들어와
    # 타이머가 영구 리셋되는 사각지대가 있어 (B)가 필요.
    NO_PROGRESS_THRESHOLD_SEC = 1800           # 30분 — 큐 완전 침묵
    # 트리클 임계: 2분간 512KB 미만 진척 → 발동 (평균 ≈ 4.3 KB/s 미만일 때만)
    # 청크 단위로 bytes_done 이 갱신되므로(transfer_pool.chunk_byte_callback),
    # 큰 파일 한 개 중간에 멈춰도 베이스라인이 안 움직여 빠르게 감지됨.
    BYTES_STALL_THRESHOLD_SEC = 120            # 2분
    BYTES_STALL_MIN_DELTA = 512 * 1024         # 512 KB

    # 창 상태 저장 파일
    _GUI_STATE_PATH = Path.home() / ".gdrive_sync" / "gui_state.json"

    # 최소 창 크기 (minsize 및 저장/복원 sanity 검증에 공용)
    _MIN_W = 720
    _MIN_H = 520

    def __init__(self, root: tk.Tk):
        self.root = root
        # 윈도우를 즉시 숨김 — 모든 위치/크기 설정이 끝나기 전에 사용자에게
        # 보여주면 pywinstyles 가 야기하는 자동 재배치(+52+52 등) 가 잠깐
        # 보였다가 마지막 위치로 점프하는 깜빡임이 생긴다. withdraw 후
        # 모든 셋업이 끝난 시점에 deiconify 로 한 번에 표시.
        self.root.withdraw()
        self.root.title(f"gdrive-sync {__version__}")
        self.root.minsize(self._MIN_W, self._MIN_H)

        # 상태 패널 접기/펼치기 토글
        self._status_expanded = False   # 기본: 접힌(한줄 요약) 모드

        # 상태
        self.log_queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.current_engine = None
        self.current_pool = None
        self._worker_lock = threading.Lock()

        # 동기화 완료 후 자동 PC 종료 플래그 (PreviewResultDialog에서 설정)
        self._shutdown_after_sync: bool = False
        # 동기화 완료 후 프로그램(GUI) 자동 종료 플래그
        self._quit_after_sync: bool = False
        # 실제 동기화(파일 전송)가 완료됐는지 (미리보기/테스트는 False)
        self._real_sync_completed: bool = False

        # 경과 시간 티커 상태
        self._sync_start_ts: Optional[float] = None
        self._elapsed_after_id: Optional[str] = None
        self._current_phase: str = ""  # 엔진이 알려준 현재 단계

        # 동기화 중 시스템 sleep 방지 (Mac/Linux/Windows). 진입/종료 자동 관리.
        self._sleep_inhibitor = SleepInhibitor()

        # 무진행 워치독 상태
        self._last_activity_ts: Optional[float] = None
        self._watchdog_triggered: bool = False
        # 바이트-진척 워치독: 베이스라인(시각, 누적 바이트) — 1MB 진척 시마다 갱신
        self._bytes_baseline_ts: Optional[float] = None
        self._bytes_baseline: int = 0

        # Claude 브랜드 테마 적용 (웜 크림 + 코랄 액센트)
        self._apply_claude_theme()

        # UI 초기화
        self._build_menubar()
        self._build_main()

        # 창 위치/크기 복원
        self._restore_window_geometry()

        # 종료 훅
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 창 크기/위치 변경 시 자동 저장 (디바운스 500ms)
        # — WM_DELETE_WINDOW를 안 거치고 종료돼도 최신 geometry가 남음
        self._geo_save_after: Optional[str] = None
        self.root.bind("<Configure>", self._on_configure_debounced)

        # 최소화→복원 시 크기 보존: 마지막으로 본 "정상" geometry를 기억했다가
        # <Map>(복원) 시 재적용. (B) Tk 가 콘텐츠 자연 크기로 줄여버리는 문제 교정.
        self._last_normal_geo: Optional[str] = None
        self.root.bind("<Unmap>", self._on_root_unmap, add="+")
        self.root.bind("<Map>", self._on_root_map, add="+")

        # 토큰 만료 임박 배너 (UI 빌드 후 체크)
        self._token_banner: Optional[tk.Frame] = None
        self.root.after(300, self._check_token_age)

        # 새 버전 확인 (하루 1회 스로틀, 백그라운드 — 실패해도 무시)
        self._update_check_running = False
        self.root.after(3000, lambda: self._start_update_check(manual=False))

        # 백그라운드 루프 시작
        self.root.after(self.QUEUE_POLL_MS, self._poll_queue)
        self.root.after(200, self._refresh_status)

        # (A) 가시성 안전망: deiconify 가 어떤 이유로든 실패해 창이 영구히
        # withdrawn(=Win+Tab엔 보이나 데스크톱엔 없음) 상태로 남는 것을 방지.
        # settle(약 550ms) 이후에도 안 떠 있으면 강제로 표시.
        self.root.after(1500, self._ensure_visible)

        # 시작 시 직전 동기화 요약을 로그에 표시 (알림 역할)
        self.root.after(500, self._show_startup_summary)

    # ──────────────────────────────────────────────
    # Claude 테마 적용
    # ──────────────────────────────────────────────

    def _apply_claude_theme(self) -> None:
        """Claude 브랜드 디자인 시스템을 Tkinter ttk 스타일에 적용."""
        C = CLAUDE_COLORS

        # 루트 창 배경
        self.root.configure(bg=C["bg_primary"])

        # Windows: pywinstyles로 타이틀바 라이트 모드 적용 (DWM API 래퍼)
        if sys.platform == "win32":
            try:
                import pywinstyles
                # Windows 11: "normal" = 라이트 크롬 (시스템 라이트 테마 따라감)
                pywinstyles.apply_style(self.root, "normal")
                # 타이틀바 헤더/타이틀 색상 명시적 지정 (Win10 대응)
                try:
                    pywinstyles.change_header_color(self.root, C["bg_primary"])
                    pywinstyles.change_title_color(self.root, C["text_primary"])
                except Exception:
                    pass
            except ImportError:
                pass   # pywinstyles 없음 → 기본 OS 크롬 유지
            except Exception:
                pass   # 구버전 Win / 권한 문제 등

        # ttk 스타일 ('clam'이 가장 커스터마이즈 가능)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        # 기본 폰트 — SF Pro (macOS) / Segoe UI (Windows) / Helvetica (Linux)
        # Tk가 SF Pro를 못 찾으면 시스템 대체 폰트로 자동 fallback
        if sys.platform == "darwin":
            base_font = ("SF Pro Text", 13)
        elif sys.platform == "win32":
            base_font = ("Segoe UI", 10)
        else:
            base_font = ("Helvetica", 11)
        bold_font = (*base_font, "bold")
        footnote_font = (base_font[0], max(base_font[1] - 2, 9), "bold")

        # ── Frame ──
        style.configure("TFrame", background=C["bg_primary"])

        # ── LabelFrame ──
        style.configure(
            "TLabelframe",
            background=C["bg_primary"],
            bordercolor=C["border"],
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=C["bg_primary"],
            foreground=C["text_secondary"],   # 액센트 → 보조 라벨 색
            font=footnote_font,
        )

        # ── Label ──
        style.configure(
            "TLabel",
            background=C["bg_primary"],
            foreground=C["text_primary"],
            font=base_font,
        )

        # ── Button (일반 — 흰 바탕 + 회색 hairline) ──
        style.configure(
            "TButton",
            background=C["bg_elevated"],
            foreground=C["text_primary"],
            bordercolor=C["border"],
            lightcolor=C["bg_elevated"],
            darkcolor=C["border"],
            focuscolor=C["accent"],
            font=base_font,
            padding=(12, 5),
            relief="solid",
            borderwidth=1,
        )
        style.map(
            "TButton",
            background=[
                ("disabled", C["bg_secondary"]),
                ("pressed", "#E5E5EA"),       # systemGray5
                ("active", "#EFEFF4"),        # 연한 hover
            ],
            foreground=[
                ("disabled", C["text_muted"]),
            ],
            bordercolor=[
                ("focus", C["accent"]),
            ],
        )

        # ── Button (강조 — systemBlue 채움, 주요 액션) ──
        style.configure(
            "Accent.TButton",
            background=C["accent"],
            foreground="#FFFFFF",
            bordercolor=C["accent"],
            lightcolor=C["accent"],
            darkcolor=C["accent_hover"],
            font=bold_font,
            padding=(14, 5),
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", C["bg_secondary"]),
                ("pressed", "#0050A8"),
                ("active", C["accent_hover"]),
            ],
            foreground=[
                ("disabled", C["text_muted"]),
            ],
        )

        # ── Entry ──
        style.configure(
            "TEntry",
            fieldbackground=C["bg_input"],
            foreground=C["text_primary"],
            bordercolor=C["border"],
            lightcolor=C["border"],
            darkcolor=C["border"],
            borderwidth=1,
            padding=6,
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", C["border_focus"])],
            lightcolor=[("focus", C["border_focus"])],
        )

        # ── Checkbutton (iOS 스위치-like: 체크 시 systemBlue 채움) ──
        style.configure(
            "TCheckbutton",
            background=C["bg_primary"],
            foreground=C["text_primary"],
            focuscolor=C["bg_primary"],    # 포커스 링 제거
            indicatorbackground=C["bg_elevated"],
            indicatorforeground=C["accent"],     # 체크 마크 색
            indicatordiameter=14,
            font=base_font,
        )
        style.map(
            "TCheckbutton",
            background=[("active", C["bg_primary"])],
            foreground=[("active", C["text_primary"])],    # hover 시 코랄→기본
            indicatorbackground=[
                ("selected", C["accent"]),          # 체크 됨 → systemBlue 배경
                ("active", C["bg_elevated"]),
            ],
            indicatorforeground=[
                ("selected", "#FFFFFF"),            # 체크 마크는 흰색
            ],
        )

        # ── Scale (슬라이더) ──
        style.configure(
            "Horizontal.TScale",
            background=C["bg_primary"],
            troughcolor=C["bg_secondary"],
            bordercolor=C["border"],
            lightcolor=C["accent"],
            darkcolor=C["accent"],
        )

        # ── Progressbar (Apple은 얇은 바 — thickness 6) ──
        style.configure(
            "Horizontal.TProgressbar",
            background=C["accent"],
            troughcolor=C["bg_secondary"],
            bordercolor=C["bg_secondary"],
            lightcolor=C["accent"],
            darkcolor=C["accent"],
            thickness=6,
        )

        # ── Scrollbar ──
        style.configure(
            "Vertical.TScrollbar",
            background=C["bg_secondary"],
            troughcolor=C["bg_primary"],
            bordercolor=C["border"],
            arrowcolor=C["text_secondary"],
            lightcolor=C["bg_secondary"],
            darkcolor=C["border"],
        )
        style.map(
            "Vertical.TScrollbar",
            background=[("active", "#E5E5EA")],
            arrowcolor=[("active", C["text_primary"])],
        )

        # ── Treeview ──
        style.configure(
            "Treeview",
            background=C["bg_elevated"],
            foreground=C["text_primary"],
            fieldbackground=C["bg_elevated"],
            bordercolor=C["border"],
            font=base_font,
            rowheight=26,
        )
        style.configure(
            "Treeview.Heading",
            background=C["bg_secondary"],
            foreground=C["text_secondary"],
            font=footnote_font,
            relief="flat",
        )
        style.map(
            "Treeview",
            background=[("selected", C["accent"])],   # systemBlue 채움
            foreground=[("selected", "#FFFFFF")],     # 흰 글자
        )
        style.map(
            "Treeview.Heading",
            background=[("active", "#E5E5EA")],
        )

        # ── Combobox ──
        style.configure(
            "TCombobox",
            fieldbackground=C["bg_input"],
            background=C["bg_elevated"],
            foreground=C["text_primary"],
            bordercolor=C["border"],
            arrowcolor=C["text_secondary"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", C["bg_input"])],
            bordercolor=[("focus", C["border_focus"])],
        )

        # ── Notebook (탭) ──
        style.configure("TNotebook", background=C["bg_primary"], bordercolor=C["border"])
        style.configure(
            "TNotebook.Tab",
            background=C["bg_secondary"],
            foreground=C["text_secondary"],
            padding=(14, 7),
            font=base_font,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", C["bg_primary"])],
            foreground=[("selected", C["accent"])],
        )

    # ──────────────────────────────────────────────
    # UI 빌더
    # ──────────────────────────────────────────────

    def _build_menubar(self) -> None:
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(
            label="동기화 폴더 관리...",
            command=self._on_manage_folders,
            accelerator="Ctrl+F",
        )
        file_menu.add_separator()
        file_menu.add_command(label="설정 파일 직접 열기 (고급)...", command=self._on_edit_config)
        file_menu.add_command(label="로컬 폴더 열기", command=self._on_open_local_folder)
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self._on_close)
        menubar.add_cascade(label="파일(F)", menu=file_menu)

        # 단축키
        self.root.bind_all("<Control-f>", lambda e: self._on_manage_folders())
        self.root.bind_all("<Control-F>", lambda e: self._on_manage_folders())

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Google 인증 (최초 1회)...", command=self._on_auth)
        tools_menu.add_command(label="연결 테스트", command=self._on_test_connection)
        tools_menu.add_separator()
        tools_menu.add_command(label="동기화 히스토리...", command=self._on_history)
        tools_menu.add_command(label="전체 로그 보기...", command=self._on_log_viewer)
        tools_menu.add_command(label="대역폭 시간대 편집...", command=self._on_bandwidth_editor)
        tools_menu.add_command(label="예약 작업 관리...", command=self._on_scheduler)
        tools_menu.add_separator()
        tools_menu.add_command(
            label="한글 파일명 일괄 정규화 (NFD → NFC)...",
            command=self._on_normalize_filenames,
        )
        tools_menu.add_separator()
        if sys.platform == "win32":
            tools_menu.add_command(label="바탕화면 바로가기 만들기", command=self._on_create_shortcut)
        tools_menu.add_separator()
        tools_menu.add_command(label="상태 초기화 (전체 재비교)...", command=self._on_reset_state)
        tools_menu.add_command(label="로그 지우기", command=self._clear_log)
        menubar.add_cascade(label="도구(T)", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="업데이트 확인...", command=self._on_check_update)
        help_menu.add_command(label="정보", command=self._on_about)
        menubar.add_cascade(label="도움말(H)", menu=help_menu)

        self.root.config(menu=menubar)

    def _build_main(self) -> None:
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)

        # ── 1) 상태 패널 (접기/펼치기 토글 지원)
        status_outer = ttk.Frame(main)
        status_outer.pack(fill="x", pady=(0, 8))

        # 헤더 행: "상태" 라벨 + 토글 버튼
        status_header = ttk.Frame(status_outer)
        status_header.pack(fill="x")

        # footnote 폰트 (플랫폼별) — LabelFrame 라벨과 일관
        if sys.platform == "darwin":
            _status_font_fn = ("SF Pro Text", 11, "bold")
        elif sys.platform == "win32":
            _status_font_fn = ("Segoe UI", 9, "bold")
        else:
            _status_font_fn = ("Helvetica", 10, "bold")

        ttk.Label(
            status_header, text="상태",
            font=_status_font_fn,
            foreground=CLAUDE_COLORS["text_secondary"],    # systemBlue 액센트 대신 보조 라벨 색
        ).pack(side="left")

        self._toggle_btn_text = tk.StringVar(value="▼ 상세 보기")
        self._toggle_btn = ttk.Button(
            status_header,
            textvariable=self._toggle_btn_text,
            command=self._toggle_status_panel,
            width=12,
            takefocus=False,
        )
        self._toggle_btn.pack(side="right")

        # 상태 내용 (Label with textvariable)
        status_frame = ttk.LabelFrame(status_outer, text="", padding=(10, 4))
        status_frame.pack(fill="x", pady=(2, 0))

        self._status_font = ("Segoe UI", 9) if sys.platform == "win32" else ("Helvetica", 11)
        self.status_var = tk.StringVar(value="로딩 중...")  # 호환용 (get_status 등)

        # 상태 내용 = Text 위젯 + Scrollbar (긴 목록일 때 스크롤 가능)
        status_text_frame = ttk.Frame(status_frame)
        status_text_frame.pack(fill="both", expand=True)

        self._status_text = tk.Text(
            status_text_frame,
            wrap="none",
            font=self._status_font,
            relief="flat",
            background=CLAUDE_COLORS["bg_primary"],
            foreground=CLAUDE_COLORS["text_primary"],
            selectbackground=CLAUDE_COLORS["accent_light"],
            selectforeground=CLAUDE_COLORS["accent"],
            height=3,       # 기본 3줄 (접힌 상태용)
            cursor="arrow",
            borderwidth=0,
            highlightthickness=0,
            padx=0, pady=0,
        )
        self._status_scrollbar = ttk.Scrollbar(
            status_text_frame, orient="vertical", command=self._status_text.yview,
        )
        self._status_text.configure(yscrollcommand=self._status_scrollbar.set)
        # 스크롤바는 기본 숨김 (접힌 상태에선 필요 없음)
        self._status_text.pack(side="left", fill="both", expand=True)
        # _status_scrollbar는 동적으로 _set_status_text에서 표시/숨김

        # Text 위젯을 읽기 전용으로 (입력 막기, 복사는 허용)
        self._status_text.bind("<Key>", lambda e: "break" if e.keysym not in (
            "Up", "Down", "Left", "Right", "Prior", "Next", "Home", "End",
            "c", "C",  # Ctrl+C 허용
        ) else None)
        self._status_text.bind("<Button-1>", lambda e: self._status_text.focus_set())

        # ── 2) 주요 액션 버튼 (grid 레이아웃 — 창 크기에 관계없이 정렬 유지)
        # Row 1: 일상 동작
        # Row 2: 고급/유틸리티
        # 두 행 모두 좌측 정렬, 창 크기 변해도 버튼 위치 고정
        action_frame = ttk.Frame(main)
        action_frame.pack(fill="x", pady=(0, 8))

        # Row 1
        # 이모지 제거: 텍스트 + 블루 액센트로 시각적 구분
        # 모든 버튼 동일 width로 grid column uniform 설정 → 격자형 정렬
        BTN_WIDTH = 12
        # uniform="action" → 같은 그룹의 컬럼들이 동일 너비로 균등 분할
        for col in range(5):
            action_frame.grid_columnconfigure(col, uniform="action")

        self.sync_btn = ttk.Button(
            action_frame, text="동기화 시작",
            command=self._on_sync_click,
            width=BTN_WIDTH,
            style="Accent.TButton",
            takefocus=False,
        )
        self.sync_btn.grid(row=0, column=0, padx=(0, 4), pady=(0, 4), sticky="ew")

        self.dryrun_btn = ttk.Button(
            action_frame, text="미리보기",
            command=lambda: self._start_sync(dry_run=True),
            width=BTN_WIDTH,
            takefocus=False,
        )
        self.dryrun_btn.grid(row=0, column=1, padx=4, pady=(0, 4), sticky="ew")

        self.test_btn = ttk.Button(
            action_frame, text="연결 테스트",
            command=self._on_test_connection,
            width=BTN_WIDTH,
            takefocus=False,
        )
        self.test_btn.grid(row=0, column=2, padx=4, pady=(0, 4), sticky="ew")

        # 3단계 중단 버튼: 폴더 후 / 파일 후 / 강제
        self.stop_after_pair_btn = ttk.Button(
            action_frame, text="폴더 후 중단",
            command=self._on_stop_after_pair, width=BTN_WIDTH, state="disabled",
            takefocus=False,
        )
        self.stop_after_pair_btn.grid(row=0, column=3, padx=4, pady=(0, 4), sticky="ew")
        _bind_tooltip(
            self.stop_after_pair_btn,
            "현재 폴더 완료 후 다음 폴더 시작 안 함.\n가장 안전 — 폴더 단위 일관성 보장."
        )

        self.stop_after_file_btn = ttk.Button(
            action_frame, text="파일 후 중단",
            command=self._on_stop_after_file, width=BTN_WIDTH, state="disabled",
            takefocus=False,
        )
        self.stop_after_file_btn.grid(row=0, column=4, padx=(4, 0), pady=(0, 4), sticky="ew")
        _bind_tooltip(
            self.stop_after_file_btn,
            "진행 중인 파일까지만 완료 후 멈춤.\n폴더 후 중단보다 빠르게 멈춤."
        )

        # 호환용 별칭 (기존 코드/state 변경 코드들이 이 이름을 씀)
        self.stop_btn = self.stop_after_pair_btn   # [중단] = 폴더 후 중단으로 매핑

        # Row 2 — 각 열이 Row 1과 정확히 세로 정렬됨 (uniform + sticky="ew")
        self.single_sync_btn = ttk.Button(
            action_frame, text="개별 동기화",
            command=self._on_single_sync, width=BTN_WIDTH,
            takefocus=False,
        )
        self.single_sync_btn.grid(row=1, column=0, padx=(0, 4), sticky="ew")

        self.single_preview_btn = ttk.Button(
            action_frame, text="개별 미리보기",
            command=self._on_single_preview, width=BTN_WIDTH,
            takefocus=False,
        )
        self.single_preview_btn.grid(row=1, column=1, padx=4, sticky="ew")

        ttk.Button(
            action_frame, text="폴더 관리",
            command=self._on_manage_folders, width=BTN_WIDTH,
            takefocus=False,
        ).grid(row=1, column=2, padx=4, sticky="ew")

        ttk.Button(
            action_frame, text="새로고침",
            command=self._refresh_status, width=BTN_WIDTH,
            takefocus=False,
        ).grid(row=1, column=3, padx=4, sticky="ew")

        # Row 2 column 4: 강제 중단 (위험 작업이라 가장 구석에 배치)
        self.force_stop_btn = ttk.Button(
            action_frame, text="강제 중단",
            command=self._on_force_stop, width=BTN_WIDTH, state="disabled",
            takefocus=False,
        )
        self.force_stop_btn.grid(row=1, column=4, padx=(4, 0), sticky="ew")
        _bind_tooltip(
            self.force_stop_btn,
            "즉시 모든 작업 중단.\n진행 중 파일이 깨질 수 있음 (위험)."
        )

        # ── 3) 빠른 설정 패널
        quick_frame = ttk.LabelFrame(main, text="  빠른 설정  ", padding=10)
        quick_frame.pack(fill="x", pady=(0, 8))

        # 대역폭 토글
        self.bw_enabled_var = tk.BooleanVar(value=False)
        bw_check = ttk.Checkbutton(
            quick_frame,
            text="대역폭 제한 사용",
            variable=self.bw_enabled_var,
            command=self._on_toggle_bandwidth,
        )
        bw_check.grid(row=0, column=0, sticky="w", padx=(0, 20))

        self.bw_info_var = tk.StringVar(value="(config.yaml의 schedule 적용)")
        ttk.Label(quick_frame, textvariable=self.bw_info_var, foreground="gray").grid(
            row=0, column=1, sticky="w",
        )

        # PC 종료 토글 (상시 변경 가능 — 동기화 도중에도 켜고 끌 수 있음)
        self.shutdown_var = tk.BooleanVar(value=False)
        # 변경 시 내부 플래그도 동기화
        def _on_shutdown_toggle():
            self._shutdown_after_sync = bool(self.shutdown_var.get())
        shutdown_check = ttk.Checkbutton(
            quick_frame,
            text="동기화 완료 후 PC 종료",
            variable=self.shutdown_var,
            command=_on_shutdown_toggle,
        )
        shutdown_check.grid(row=0, column=2, sticky="w")
        _bind_tooltip(
            shutdown_check,
            "동기화 완료 후 확인 없이 즉시 PC 종료 예약.\n"
            "60초 카운트다운 — 상태바 [취소] 버튼으로 취소 가능.\n"
            "동기화 중에도 켜고 끌 수 있으며, 끝나는 순간의 상태 기준으로 동작.\n"
            "※ macOS: sudo 권한 없으면 시스템 다이얼로그가 뜨므로 무인 종료가 안 될 수 있음."
        )

        # 프로그램(GUI) 종료 토글 (PC 종료의 가벼운 버전 — 상시 변경 가능)
        self.quit_var = tk.BooleanVar(value=False)
        def _on_quit_toggle():
            self._quit_after_sync = bool(self.quit_var.get())
        quit_check = ttk.Checkbutton(
            quick_frame,
            text="동기화 완료 후 프로그램 종료",
            variable=self.quit_var,
            command=_on_quit_toggle,
        )
        quit_check.grid(row=0, column=3, sticky="w", padx=(16, 0))
        _bind_tooltip(
            quit_check,
            "동기화 완료 후 이 프로그램(GUI)을 자동으로 종료합니다.\n"
            "PC는 켜두되, 프로그램만 닫아 절전이 정상 작동하게 합니다.\n"
            "※ macOS 중요: GUI를 띄운 런처(launch-gui.app)가 살아 있으면\n"
            "   맥북이 절전에 못 들어가 배터리가 소모됩니다. 종료 시 런처까지\n"
            "   함께 정리해 뚜껑을 닫으면 정상 절전됩니다.\n"
            "'PC 종료'와 함께 켜면 PC 종료가 우선합니다."
        )

        # 병렬 전송 슬라이더
        ttk.Label(quick_frame, text="동시 전송 파일 수:").grid(
            row=1, column=0, sticky="w", pady=(8, 0),
        )
        self.parallel_var = tk.IntVar(value=5)
        self.parallel_scale = ttk.Scale(
            quick_frame, from_=1, to=10, orient="horizontal",
            variable=self.parallel_var,
            command=self._on_parallel_change,
            length=300,
        )
        self.parallel_scale.grid(row=1, column=1, sticky="ew", padx=(10, 10), pady=(8, 0))
        self.parallel_label_var = tk.StringVar(value="5 개")
        ttk.Label(quick_frame, textvariable=self.parallel_label_var, width=6).grid(
            row=1, column=2, sticky="w", pady=(8, 0),
        )
        quick_frame.columnconfigure(1, weight=1)

        # 악성 의심 파일 다운로드 토글 (config의 acknowledge_abuse와 양방향 동기화)
        self.abuse_var = tk.BooleanVar(value=False)   # _refresh_quick_settings에서 cfg값 반영
        abuse_check = ttk.Checkbutton(
            quick_frame,
            text="⚠ Drive 차단 파일 강제 다운로드 (악성 의심)",
            variable=self.abuse_var,
            command=self._on_toggle_abuse,
        )
        abuse_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        _bind_tooltip(
            abuse_check,
            "Google Drive가 '악성 의심'으로 분류해 다운로드를 차단한 파일도\n"
            "본인 소유 시 강제로 받습니다 (HTTP 403 cannotDownloadAbusiveFile 우회).\n"
            "오래된 ZIP, 메신저 설치파일 등이 false positive로 잡힐 때 사용.\n"
            "⚠ 사용자 책임 — 정말 안전한 파일인지 확인 후 켜세요.\n"
            "체크 상태는 config.yaml에 즉시 저장되어 다음 실행에도 유지됩니다."
        )

        # 자동 한글 파일명 정규화 토글 (auto_normalize_filenames)
        self.normalize_var = tk.BooleanVar(value=False)
        normalize_check = ttk.Checkbutton(
            quick_frame,
            text="🔤 동기화 시작 전 한글 파일명 자동 정규화 (NFD → NFC)",
            variable=self.normalize_var,
            command=self._on_toggle_normalize,
        )
        normalize_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))
        _bind_tooltip(
            normalize_check,
            "macOS에서 만들어진 파일이 Windows에서 한글 자모가 분절돼 보이는 문제\n"
            "(NFD/NFC 차이)를 매 동기화 시작 전 자동으로 해결합니다.\n"
            "수동 일괄 정규화는 [도구 → 한글 파일명 일괄 정규화] 메뉴를 사용하세요.\n"
            "체크 상태는 config.yaml에 즉시 저장됩니다."
        )

        # ── 4.5) 진행 상황 (동기화 중에만 표시) — 2단 진행 + ETA
        self._progress_container = ttk.Frame(main)
        self._progress_container.pack(fill="x")

        self._progress_inner = ttk.LabelFrame(
            self._progress_container, text="  진행 상황  ", padding=(10, 6),
        )
        # pack 안 함 — _show_progress()에서 동적으로 표시

        # 전체 진행 (B3+ 추정/확정 분모 기반)
        self._overall_label = tk.StringVar(value="")
        ttk.Label(
            self._progress_inner, textvariable=self._overall_label,
            font=self._status_font,
        ).pack(anchor="w")
        self._overall_bar = ttk.Progressbar(
            self._progress_inner, mode="determinate", maximum=100,
        )
        self._overall_bar.pack(fill="x", pady=(0, 6))

        # 현재 폴더 진행 (기존 progress_bar의 역할)
        self._progress_label = tk.StringVar(value="")
        ttk.Label(
            self._progress_inner, textvariable=self._progress_label,
            font=self._status_font,
        ).pack(anchor="w")
        self._progress_bar = ttk.Progressbar(
            self._progress_inner, mode="determinate", maximum=100,
        )
        self._progress_bar.pack(fill="x", pady=(0, 6))

        # 통계 라인: 파일/바이트, 속도, ETA
        self._stats_label = tk.StringVar(value="")
        ttk.Label(
            self._progress_inner, textvariable=self._stats_label,
            font=self._status_font,
            foreground=CLAUDE_COLORS.get("text_secondary", "#666"),
        ).pack(anchor="w")
        self._eta_label = tk.StringVar(value="")
        ttk.Label(
            self._progress_inner, textvariable=self._eta_label,
            font=self._status_font,
            foreground=CLAUDE_COLORS.get("text_secondary", "#666"),
        ).pack(anchor="w")

        # ── 5) 로그 패널
        log_frame = ttk.LabelFrame(main, text="  로그  ", padding=4)
        log_frame.pack(fill="both", expand=True)

        log_inner = ttk.Frame(log_frame)
        log_inner.pack(fill="both", expand=True)

        log_font = ("Consolas", 9) if sys.platform == "win32" else ("Menlo", 11)

        self.log_text = tk.Text(
            log_inner, wrap="word", height=14,
            font=log_font,
            state="disabled",
            background=CLAUDE_COLORS["log_bg"],
            foreground=CLAUDE_COLORS["log_fg"],
            insertbackground=CLAUDE_COLORS["log_fg"],
            selectbackground=CLAUDE_COLORS["accent"],
            selectforeground="#FFFFFF",
            relief="flat",
            padx=8, pady=6,
            borderwidth=0,
            highlightthickness=0,
        )
        scrollbar = ttk.Scrollbar(log_inner, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

        # 색상 태그 (Claude 웜 톤)
        self.log_text.tag_configure("DEBUG", foreground=CLAUDE_COLORS["log_debug"])
        self.log_text.tag_configure("INFO", foreground=CLAUDE_COLORS["log_info"])
        self.log_text.tag_configure("WARNING", foreground=CLAUDE_COLORS["log_warning"])
        self.log_text.tag_configure("ERROR", foreground=CLAUDE_COLORS["log_error"])
        self.log_text.tag_configure("CRITICAL", foreground=CLAUDE_COLORS["log_error"],
                                     font=(log_font[0], log_font[1], "bold"))
        self.log_text.tag_configure("SUCCESS", foreground=CLAUDE_COLORS["log_success"],
                                     font=(log_font[0], log_font[1], "bold"))
        self.log_text.tag_configure("HEADER", foreground=CLAUDE_COLORS["log_header"],
                                     font=(log_font[0], log_font[1], "bold"))

        # ── 5) 상태 바 (Claude 테마) — Frame 감싸서 라벨 + 종료 취소 버튼 함께
        statusbar_frame = tk.Frame(
            self.root,
            bg=CLAUDE_COLORS["bg_secondary"],
            highlightthickness=1,
            highlightbackground=CLAUDE_COLORS["border"],
        )
        statusbar_frame.pack(side="bottom", fill="x")

        self.statusbar_var = tk.StringVar(value="대기 중")
        statusbar = tk.Label(
            statusbar_frame, textvariable=self.statusbar_var,
            bg=CLAUDE_COLORS["bg_secondary"],
            fg=CLAUDE_COLORS["text_secondary"],
            relief="flat", anchor="w", padx=10, pady=4,
            font=("Segoe UI", 9) if sys.platform == "win32" else ("Helvetica", 10),
            borderwidth=0,
        )
        statusbar.pack(side="left", fill="x", expand=True)

        # PC 종료 취소 버튼 — 평소 숨김, 카운트다운 중에만 노출
        self._shutdown_cancel_btn = tk.Button(
            statusbar_frame, text="❌ PC 종료 취소",
            bg="#FF3B30", fg="white",
            activebackground="#D32F2F", activeforeground="white",
            relief="flat", borderwidth=0,
            padx=12, pady=2,
            font=("Segoe UI", 9, "bold") if sys.platform == "win32" else ("Helvetica", 10, "bold"),
            cursor="hand2",
            command=self._cancel_shutdown,
        )
        # pack 안 함 — _start_shutdown_countdown 에서 동적으로
        self._shutdown_after_id: Optional[str] = None
        self._shutdown_remaining = 0
        self._shutdown_cancel_cmd = ""

    # ──────────────────────────────────────────────
    # 상태 갱신
    # ──────────────────────────────────────────────

    def _toggle_status_panel(self) -> None:
        """상태 패널 접기/펼치기 토글."""
        self._status_expanded = not self._status_expanded
        if self._status_expanded:
            self._toggle_btn_text.set("▲ 접기")
        else:
            self._toggle_btn_text.set("▼ 상세 보기")
        self._refresh_status()

    # ──────────────────────────────────────────────
    # 상태 Text 위젯 조작
    # ──────────────────────────────────────────────
    _COMPACT_MAX_HEIGHT = 6      # 접힌 상태 최대 줄 수 (폴더 약 5개 + 요약)
    _EXPANDED_MAX_HEIGHT = 10    # 펼친 상태 최대 줄 수 (폴더 약 5개 × 2줄)

    def _set_status_text(self, text: str) -> None:
        """상태 패널 Text 위젯에 내용 반영 + 줄 수에 맞춰 높이 조정.

        - 접힌 상태: 내용 줄 수 만큼만 (최대 12줄)
        - 펼친 상태: 최대 16줄까지 표시, 그 이상이면 스크롤 활성화
        """
        self._status_text.config(state="normal")
        self._status_text.delete("1.0", "end")
        self._status_text.insert("1.0", text)

        # 줄 수 계산 (랩 안 하므로 \n 개수 + 1)
        num_lines = text.count("\n") + 1

        max_height = (
            self._EXPANDED_MAX_HEIGHT if self._status_expanded
            else self._COMPACT_MAX_HEIGHT
        )
        target_height = min(num_lines, max_height)
        target_height = max(1, target_height)
        self._status_text.config(height=target_height)

        # 스크롤바 표시/숨김
        if num_lines > max_height:
            self._status_scrollbar.pack(side="right", fill="y")
        else:
            self._status_scrollbar.pack_forget()

        self._status_text.config(state="disabled")

    def _refresh_status(self) -> None:
        """상태 패널 갱신. 접힌/펼친 모드에 따라 표시 분기."""
        try:
            cfg = load_config()

            if self._status_expanded:
                text = self._build_status_expanded(cfg)
            else:
                text = self._build_status_compact(cfg)

            self._set_status_text(text)

            # 빠른 설정 위젯 동기화
            self.bw_enabled_var.set(cfg.bandwidth.enabled)
            self.parallel_var.set(cfg.performance.parallel_transfers)
            self.parallel_label_var.set(f"{cfg.performance.parallel_transfers} 개")
            self.abuse_var.set(cfg.acknowledge_abuse)
            self.normalize_var.set(getattr(cfg, "auto_normalize_filenames", False))

            limiter = make_limiter(cfg.bandwidth)
            if cfg.bandwidth.enabled and limiter:
                s = limiter.get_status()
                up = "무제한" if s["upload_unlimited"] else f"↑{s['upload_mbps']:.1f}"
                dn = "무제한" if s["download_unlimited"] else f"↓{s['download_mbps']:.1f}"
                self.bw_info_var.set(f"(현재 규칙: {s['active_rule']}, {up} / {dn} MB/s)")
            else:
                self.bw_info_var.set("(제한 없이 전속력)")

        except FileNotFoundError:
            self._set_status_text(
                "⚠  설정 파일이 없습니다.\n"
                "   py -3.14 -m gdrive_sync init  (Windows)\n"
                "   python3 -m gdrive_sync init   (macOS)"
            )
        except Exception as e:
            self._set_status_text(f"⚠  설정 로드 오류: {e}")

        # 다음 자동 갱신 예약
        self.root.after(self.REFRESH_INTERVAL_MS, self._refresh_status)

    def _build_status_compact(self, cfg) -> str:
        """접힌 상태: 폴더당 한 줄 + 요약 한 줄."""
        lines = []
        for pair in cfg.sync_pairs:
            st = load_state(pair.local_path)
            count = len(st.files)
            # 마지막 동기화 시각: 짧게 표시
            last = st.last_sync
            if last:
                # "2026-04-15T08:06:19Z" → "04-15 08:06"
                try:
                    last_short = last[5:16].replace("T", " ")
                except (IndexError, TypeError):
                    last_short = last[:16]
            else:
                last_short = "미동기화"
            # 로컬 경로: 길면 폴더명만
            local_name = pair.local_path.name or str(pair.local_path)
            # 리모트 경로: 길면 마지막 부분만
            remote_parts = pair.remote_path.split("/")
            remote_short = remote_parts[-1] if remote_parts else pair.remote_path
            warn = " ⚠" if not pair.local_path.exists() else ""
            lines.append(f"📁 {local_name} ↔ {remote_short} — {count}개 ({last_short}){warn}")

        # 대역폭 / 병렬 / 인증을 한 줄에 요약
        limiter = make_limiter(cfg.bandwidth)
        parts = []
        if limiter:
            s = limiter.get_status()
            up = "무제한" if s["upload_unlimited"] else f"↑{s['upload_mbps']:.1f}"
            dn = "무제한" if s["download_unlimited"] else f"↓{s['download_mbps']:.1f}"
            parts.append(f"⚡{s['active_rule']} {up}/{dn}")
        else:
            parts.append("⚡전속력")
        parts.append(f"🧵{cfg.performance.parallel_transfers}")
        auth_ok = DEFAULT_TOKEN_PATH.exists()
        parts.append(f"🔑{'✓' if auth_ok else '✕'}")
        lines.append("  ".join(parts))

        return "\n".join(lines)

    def _build_status_expanded(self, cfg) -> str:
        """펼친 상태: compact 헤더 + 풀 경로/크기/풀 시각 추가 (폴더당 2줄)."""
        from gdrive_sync.utils import human_size

        lines = []
        for pair in cfg.sync_pairs:
            local_name = pair.local_path.name or str(pair.local_path)
            remote_parts = pair.remote_path.split("/")
            remote_short = remote_parts[-1] if remote_parts else pair.remote_path

            st = load_state(pair.local_path)
            count = len(st.files)
            total_size = sum((fs.local_size or 0) for fs in st.files.values())

            # 마지막 동기화 시각 (풀 형식 - compact는 "MM-DD HH:MM", 여기는 초까지)
            last = st.last_sync
            if last and len(last) >= 19:
                last_full = last[:19].replace("T", " ")  # "2026-04-16 11:06:55"
            elif last:
                last_full = last
            else:
                last_full = "미동기화"

            warn = " ⚠" if not pair.local_path.exists() else ""

            # Line 1: compact-style 헤더 + 크기/풀 시각
            size_str = f"{human_size(total_size)}" if total_size > 0 else "0 B"
            header = f"📁 {local_name} ↔ {remote_short} — {count}개"
            if count > 0:
                header += f" · {size_str}"
            header += f" · {last_full}{warn}"
            lines.append(header)

            # Line 2: 풀 경로 (로컬 → 리모트)
            lines.append(f"    📍 {pair.local_path}  →  {pair.remote_path}")

        lines.append("")  # 대역폭 섹션 앞 구분

        # 대역폭 / 병렬 / 인증
        limiter = make_limiter(cfg.bandwidth)
        if limiter:
            s = limiter.get_status()
            up = "무제한" if s["upload_unlimited"] else f"{s['upload_mbps']:.1f} MB/s"
            dn = "무제한" if s["download_unlimited"] else f"{s['download_mbps']:.1f} MB/s"
            lines.append(f"⚡ 대역폭 [{s['active_rule']}]    ↑ {up}    ↓ {dn}")
        else:
            lines.append("⚡ 대역폭 제한 없음 (전속력)")

        lines.append(f"🧵 병렬 전송: {cfg.performance.parallel_transfers}개")
        auth_ok = DEFAULT_TOKEN_PATH.exists()
        auth_msg = "인증됨" if auth_ok else "인증 필요 (도구 → Google 인증)"
        lines.append(f"🔑 {'✓' if auth_ok else '✕'} {auth_msg}")

        return "\n".join(lines)

    # ──────────────────────────────────────────────
    # 동기화 실행 (워커 스레드)
    # ──────────────────────────────────────────────

    def _on_sync_click(self) -> None:
        """'동기화' 버튼 클릭: 옵션 다이얼로그 → _start_sync 호출."""
        # 사전 체크
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("알림", "이미 작업이 실행 중입니다.")
            return
        if not DEFAULT_TOKEN_PATH.exists():
            messagebox.showwarning(
                "인증 필요",
                "Google 인증이 필요합니다.\n\n메뉴: 도구 → Google 인증 (최초 1회)",
            )
            return

        dlg = SyncOptionsDialog(self.root, title="동기화 옵션")
        self.root.wait_window(dlg)
        if not dlg.confirmed:
            return
        self._start_sync(
            dry_run=False,
            force_mode=dlg.force_mode,
            shutdown_after=dlg.shutdown_after,
        )

    def _start_sync(
        self,
        dry_run: bool,
        force_mode: Optional[str] = None,
        shutdown_after: bool = False,
    ) -> None:
        with self._worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                messagebox.showinfo("알림", "이미 작업이 실행 중입니다.")
                return

            # 인증 확인
            if not DEFAULT_TOKEN_PATH.exists():
                messagebox.showwarning(
                    "인증 필요",
                    "Google 인증이 필요합니다.\n\n"
                    "메뉴: 도구 → Google 인증 (최초 1회)",
                )
                return

            # PC 종료 예약 플래그 (체크박스도 같이 켜서 동기화 중 변경 가능하게)
            if shutdown_after and not dry_run:
                self._shutdown_after_sync = True
                try:
                    self.shutdown_var.set(True)
                except Exception:
                    pass

            mode_label = {
                "upload": " (업로드만)",
                "download": " (다운로드만)",
                None: "",
            }.get(force_mode, "")

            self._set_busy(
                True,
                f"동기화 실행 중{mode_label}" if not dry_run else "미리보기 실행 중",
            )
            self._log_header(
                f"=== 동기화 시작{mode_label} ==="
                if not dry_run
                else "=== 미리보기 (DRY-RUN) ==="
            )

            self.worker_thread = threading.Thread(
                target=self._sync_worker,
                args=(dry_run, force_mode),
                daemon=True,
                name="gdrv-gui-sync",
            )
            self.worker_thread.start()

    def _sync_worker(self, dry_run: bool, force_mode: Optional[str] = None) -> None:
        """실제 동기화. 워커 스레드에서 실행."""
        # 로깅을 GUI 큐로 리다이렉트
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        prev_level = root_logger.level
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        try:
            from gdrive_sync.drive_api import DriveClient
            from gdrive_sync.sync_engine import SyncEngine

            cfg = load_config()
            drive = DriveClient(
                cfg.network,
                interactive_auth=False,
                performance=cfg.performance,
                acknowledge_abuse=cfg.acknowledge_abuse,
            )
            if cfg.acknowledge_abuse:
                self.log_queue.put((
                    "log", "WARNING",
                    "⚠ Drive 차단 파일 강제 다운로드 활성 — 악성 의심 파일도 받습니다",
                ))
            limiter = make_limiter(cfg.bandwidth)
            if limiter:
                st = limiter.get_status()
                self.log_queue.put((
                    "log", "INFO",
                    f"대역폭 제한: [{st['active_rule']}] "
                    f"↑ {st['upload_mbps']} MB/s / ↓ {st['download_mbps']} MB/s"
                ))

            def _progress_cb(completed, total, rel_path):
                self.log_queue.put(("progress", completed, f"{total}|{rel_path}"))

            def _status_cb(phase_text):
                self.log_queue.put(("status", phase_text, None))

            # 전체 진행 추적기 + ETA 계산기 (B3+ 방식)
            self._progress_tracker = ProgressTracker(cfg)
            self._eta_calc = ETACalculator()

            engine = SyncEngine(
                cfg=cfg,
                drive=drive,
                dry_run=dry_run,
                force_mode=force_mode,
                progress_factory=None,        # GUI에선 tqdm 사용 안 함
                bandwidth_limiter=limiter,
                progress_callback=_progress_cb,
                status_callback=_status_cb,
                progress_tracker=self._progress_tracker,
            )
            self.current_engine = engine

            start = time.time()
            results = engine.run()
            elapsed = time.time() - start

            # 요약 메시지
            summary_lines = ["", "━━━ 완료 요약 ━━━"]
            total_up = sum(s.uploaded for _, s in results)
            total_dn = sum(s.downloaded for _, s in results)
            total_up_bytes = sum(s.uploaded_bytes for _, s in results)
            total_dn_bytes = sum(s.downloaded_bytes for _, s in results)
            total_del_l = sum(s.deleted_local for _, s in results)
            total_del_r = sum(s.deleted_remote for _, s in results)
            total_err = sum(s.errors for _, s in results)
            total_vanished = sum(getattr(s, "vanished", 0) for _, s in results)
            total_removed_state = sum(getattr(s, "removed_state", 0) for _, s in results)
            vanished_samples_total: dict[str, int] = {}
            action_summary_total: dict[str, int] = {}
            for _, s in results:
                for name, cnt in getattr(s, "vanished_samples", {}).items():
                    vanished_samples_total[name] = (
                        vanished_samples_total.get(name, 0) + cnt
                    )
                for a in s.actions:
                    k = a.type.value
                    action_summary_total[k] = action_summary_total.get(k, 0) + 1

            if dry_run:
                counts: dict[str, int] = {}
                for _, s in results:
                    for a in s.actions:
                        counts[a.type.value] = counts.get(a.type.value, 0) + 1
                for k, v in sorted(counts.items()):
                    summary_lines.append(f"  {k:<22} {v}개")
                summary_lines.append(f"  (실제 전송 없음)")
            else:
                summary_lines.append(f"  ↑ 업로드:    {total_up}개")
                summary_lines.append(f"  ↓ 다운로드:  {total_dn}개")
                summary_lines.append(f"  ✕ 로컬삭제:  {total_del_l}개")
                summary_lines.append(f"  ✕ 리모트삭제: {total_del_r}개")
                total_mv_r = sum(getattr(s, "moved_remote", 0) for _, s in results)
                total_mv_l = sum(getattr(s, "moved_local", 0) for _, s in results)
                total_prune_r = sum(getattr(s, "pruned_remote_dirs", 0) for _, s in results)
                if total_mv_r or total_mv_l:
                    summary_lines.append(
                        f"  ➜ 이동감지:  {total_mv_r + total_mv_l}개 "
                        f"(Drive {total_mv_r} / 로컬 {total_mv_l} — 재전송 생략)"
                    )
                if total_prune_r:
                    summary_lines.append(f"  🗑 Drive빈폴더: {total_prune_r}개 정리")
                total_norm_r = sum(getattr(s, "normalized_remote", 0) for _, s in results)
                if total_norm_r:
                    summary_lines.append(
                        f"  🔤 Drive명정규화: {total_norm_r}개 (NFD→NFC)"
                    )
                if total_removed_state > 0:
                    # 양쪽 사라짐 — 정상일 수도, ghost-cleanup 의심일 수도. 가시화 필수.
                    summary_lines.append(f"  ⊖ state정리: {total_removed_state}개 (양쪽 사라짐)")
                if total_err:
                    summary_lines.append(f"  ⚠ 오류:     {total_err}개")
                if total_vanished:
                    summary_lines.append(
                        f"  ⊘ 사라진 파일: {total_vanished}개 "
                        f"(백신/DLP 임시파일 추정 — 정상)"
                    )
                    from gdrive_sync.sync_engine import format_vanished_samples
                    sample_str = format_vanished_samples(vanished_samples_total)
                    if sample_str:
                        summary_lines.append(f"     예: {sample_str}")
                summary_lines.append(f"  ⏱ 소요:     {elapsed:.1f}초")

            for line in summary_lines:
                self.log_queue.put(("log", "SUCCESS" if not total_err else "WARNING", line))

            # 미리보기 완료: 결과 다이얼로그로 확인 후 동기화 진행 여부 선택
            if dry_run:
                all_pair_indices = list(range(len(cfg.sync_pairs)))
                self.root.after(
                    100,
                    lambda r=results, i=all_pair_indices:
                        self._show_preview_result(r, i),
                )

            # 실제 동기화 완료 플래그 (히스토리/PC종료 표시용)
            if not dry_run:
                self._real_sync_completed = True
                try:
                    notify_sync_complete(total_up, total_dn, total_err, elapsed)
                except Exception:
                    pass

            # Feature 2: 히스토리 기록
            if not dry_run:
                try:
                    record = SyncRecord(
                        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        uploaded=total_up,
                        uploaded_bytes=total_up_bytes,
                        downloaded=total_dn,
                        downloaded_bytes=total_dn_bytes,
                        deleted=total_del_l + total_del_r,
                        removed_state=total_removed_state,
                        errors=total_err,
                        elapsed_sec=elapsed,
                        pairs_count=len(results),
                        dry_run=False,
                        pair_paths=[str(p.local_path) for p, _ in results],
                        action_summary=action_summary_total,
                    )
                    append_record(record)
                except Exception:
                    pass

            self.log_queue.put(("done", None, None))

        except AuthError as e:
            # OAuth 관련 에러는 한글 원인+해결책으로 풀어서 표시
            log.exception("인증 에러")
            self.log_queue.put((
                "log", "ERROR",
                f"❌ 인증 실패\n{humanize_oauth_error(e)}",
            ))
        except Exception as e:
            log.exception("동기화 실패")
            # invalid_grant 같은 OAuth 시그니처가 raw error에 섞인 경우도 한글화
            human = humanize_oauth_error(e)
            if "알 수 없는" in human:
                self.log_queue.put(("log", "ERROR", f"❌ 동기화 실패: {e}"))
            else:
                self.log_queue.put((
                    "log", "ERROR",
                    f"❌ 동기화 실패\n{human}",
                ))

            # 실패 시에도 토스트 + 히스토리
            _elapsed = time.time() - start if 'start' in locals() else 0
            try:
                notify_sync_complete(0, 0, 1, _elapsed)
            except Exception:
                pass
            try:
                record = SyncRecord(
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    errors=1,
                    elapsed_sec=_elapsed,
                    dry_run=dry_run,
                )
                append_record(record)
            except Exception:
                pass

            self.log_queue.put(("done", None, None))
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(prev_level)
            self.current_engine = None

    def _on_stop_after_pair(self) -> None:
        """폴더 후 중단 — 현재 폴더의 모든 파일 끝까지 → 다음 폴더 안 시작.

        가장 안전. 폴더 단위 일관성 보장.
        """
        engine = self.current_engine
        if not engine:
            return
        self.log_queue.put(("log", "WARNING",
            "⏹ 폴더 후 중단 — 현재 폴더 완료 후 멈춥니다. (더 빨리: 파일 후 중단 / 즉시: 강제 중단)"))
        try:
            engine.request_stop_after_pair()
        except Exception:
            pass
        self.stop_after_pair_btn.config(state="disabled")
        # 파일/강제 단계는 격상 가능하므로 활성 유지

    def _on_stop_after_file(self) -> None:
        """파일 후 중단 — 진행 중 파일은 끝, 새 파일/폴더 안 시작."""
        engine = self.current_engine
        if not engine:
            return
        self.log_queue.put(("log", "WARNING",
            "⏹ 파일 후 중단 — 진행 중 파일 완료 후 멈춥니다. (즉시: 강제 중단)"))
        try:
            engine.request_stop_after_file()
        except Exception:
            pass
        self.stop_after_file_btn.config(state="disabled")
        self.stop_after_pair_btn.config(state="disabled")
        # 강제 중단으로 격상 가능

    def _on_force_stop(self) -> None:
        """강제 중단 — 즉시 모든 작업 중단."""
        engine = self.current_engine
        if not engine:
            return
        if not messagebox.askyesno(
            "강제 중단",
            "전송 중인 파일을 즉시 중단합니다.\n"
            "진행 중인 파일이 깨지거나 일부만 업로드된 상태로 남을 수 있습니다.\n"
            "중단된 파일은 다음 동기화에서 다시 시도됩니다.\n\n"
            "강제 중단하시겠습니까?",
        ):
            return
        self.log_queue.put(("log", "ERROR", "⚡ 강제 중단 — 모든 작업을 즉시 중지합니다."))
        try:
            engine.request_force_stop()
        except Exception:
            pass
        self.force_stop_btn.config(state="disabled")
        self.stop_after_file_btn.config(state="disabled")
        self.stop_after_pair_btn.config(state="disabled")

    # 호환용 별칭
    def _on_stop(self) -> None:
        """기존 [중단] 이름 호환 — 폴더 후 중단으로 매핑."""
        self._on_stop_after_pair()

    # ──────────────────────────────────────────────
    # 연결 테스트 (워커 스레드)
    # ──────────────────────────────────────────────

    def _on_test_connection(self) -> None:
        with self._worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                messagebox.showinfo("알림", "이미 작업이 실행 중입니다.")
                return

            self._set_busy(True, "연결 테스트 중...")
            self._log_header("=== 연결 테스트 ===")

            self.worker_thread = threading.Thread(
                target=self._test_worker, daemon=True, name="gdrv-gui-test",
            )
            self.worker_thread.start()

    def _test_worker(self) -> None:
        try:
            from gdrive_sync.network import test_connections
            try:
                cfg = load_config()
                net = cfg.network
            except FileNotFoundError:
                from gdrive_sync.config import NetworkConfig
                net = NetworkConfig()

            results = test_connections(net)
            for r in results:
                icon = "✓" if r.ok else "✕"
                level = "INFO" if r.ok else "ERROR"
                detail = f"{r.elapsed_ms}ms  {r.detail}" if r.elapsed_ms else r.detail
                self.log_queue.put(("log", level, f"  {icon}  {r.method:<24} {detail}"))

            ok = any(r.method.startswith("direct") and r.ok for r in results)
            if ok:
                self.log_queue.put(("log", "SUCCESS", "→ 직접 연결 가능. 프록시 불필요."))
            else:
                self.log_queue.put(("log", "WARNING", "→ 직접 연결 불가. config.yaml의 network 섹션 확인 필요."))
        except Exception as e:
            self.log_queue.put(("log", "ERROR", f"연결 테스트 실패: {e}"))
        finally:
            self.log_queue.put(("done", None, None))

    # ──────────────────────────────────────────────
    # 인증 (워커 스레드 — 브라우저 플로우)
    # ──────────────────────────────────────────────

    def _on_auth(self) -> None:
        from gdrive_sync.auth import USER_CREDENTIALS_PATH, resolve_credentials_path
        if not resolve_credentials_path().exists():
            messagebox.showwarning(
                "credentials.json 필요",
                "credentials.json 파일을 찾지 못했습니다.\n\n"
                "Google Cloud Console에서 OAuth 데스크톱 앱을 만들고\n"
                "다운로드한 JSON을 'credentials.json'으로 이름 변경해\n"
                f"아래 위치에 저장한 후 다시 시도하세요:\n"
                f"  {USER_CREDENTIALS_PATH}\n\n"
                "자세한 방법: docs/INSTALL.md 참고",
            )
            return

        self._log_header("=== Google 인증 ===")
        self._log("브라우저가 열립니다. 로그인 후 '허용' 버튼을 누르세요.", "INFO")
        self._set_busy(True, "인증 대기 중 (브라우저 확인)...")

        def worker():
            try:
                creds = load_credentials(interactive=True)
                self.log_queue.put(("log", "SUCCESS", f"✓ 인증 성공 (유효: {creds.valid})"))
                self.log_queue.put(("auth_ok", None, None))
            except Exception as e:
                self.log_queue.put(("log", "ERROR", f"❌ 인증 실패: {e}"))
            finally:
                self.log_queue.put(("done", None, None))

        self.worker_thread = threading.Thread(target=worker, daemon=True, name="gdrv-gui-auth")
        self.worker_thread.start()

    # ──────────────────────────────────────────────
    # 빠른 설정 핸들러
    # ──────────────────────────────────────────────

    def _on_toggle_bandwidth(self) -> None:
        """대역폭 제한 체크박스 토글 → config.yaml에 즉시 반영."""
        try:
            cfg = load_config()
            new_enabled = self.bw_enabled_var.get()
            if cfg.bandwidth.enabled == new_enabled:
                return
            # raw dict로 로드해서 enabled만 수정
            import yaml
            with open(cfg.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if "bandwidth" not in raw:
                raw["bandwidth"] = {}
            raw["bandwidth"]["enabled"] = new_enabled
            save_config(raw, cfg.config_path)
            self._log(
                f"⚙ 대역폭 제한 {'활성화' if new_enabled else '비활성화'} 저장됨",
                "INFO",
            )
            self._refresh_status()
        except Exception as e:
            messagebox.showerror("저장 실패", f"설정 저장 실패: {e}")

    def _on_toggle_abuse(self) -> None:
        """악성 의심 파일 다운로드 토글 → config.yaml에 즉시 반영."""
        try:
            cfg = load_config()
            new_val = bool(self.abuse_var.get())
            if cfg.acknowledge_abuse == new_val:
                return
            import yaml
            with open(cfg.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            raw["acknowledge_abuse"] = new_val
            save_config(raw, cfg.config_path)
            if new_val:
                self._log(
                    "⚠ Drive 차단 파일 강제 다운로드 활성화 — 본인 책임 하 진행",
                    "WARNING",
                )
            else:
                self._log("⚙ Drive 차단 파일 강제 다운로드 비활성화 (안전 기본값)", "INFO")
        except Exception as e:
            messagebox.showerror("저장 실패", f"설정 저장 실패: {e}")
            # 실패 시 체크박스 상태 되돌리기
            try:
                cfg = load_config()
                self.abuse_var.set(cfg.acknowledge_abuse)
            except Exception:
                pass

    def _on_toggle_normalize(self) -> None:
        """자동 한글 파일명 정규화 토글 → config.yaml에 즉시 반영."""
        try:
            cfg = load_config()
            new_val = bool(self.normalize_var.get())
            if getattr(cfg, "auto_normalize_filenames", False) == new_val:
                return
            import yaml
            with open(cfg.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            raw["auto_normalize_filenames"] = new_val
            save_config(raw, cfg.config_path)
            if new_val:
                self._log(
                    "🔤 동기화 시작 전 한글 파일명 자동 정규화 활성화",
                    "INFO",
                )
            else:
                self._log("🔤 자동 한글 파일명 정규화 비활성화", "INFO")
        except Exception as e:
            messagebox.showerror("저장 실패", f"설정 저장 실패: {e}")
            try:
                cfg = load_config()
                self.normalize_var.set(getattr(cfg, "auto_normalize_filenames", False))
            except Exception:
                pass

    def _on_parallel_change(self, value: str) -> None:
        """슬라이더 값 변경 표시만 (저장은 버튼으로)."""
        n = int(float(value))
        self.parallel_label_var.set(f"{n} 개")

        # 디바운스: 슬라이더 드래그가 끝났을 때만 저장
        if hasattr(self, "_parallel_save_after"):
            self.root.after_cancel(self._parallel_save_after)
        self._parallel_save_after = self.root.after(800, lambda: self._save_parallel(n))

    def _save_parallel(self, n: int) -> None:
        try:
            cfg = load_config()
            if cfg.performance.parallel_transfers == n:
                return
            import yaml
            with open(cfg.config_path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            if "performance" not in raw:
                raw["performance"] = {}
            raw["performance"]["parallel_transfers"] = n
            save_config(raw, cfg.config_path)
            self._log(f"⚙ 병렬 전송 수 → {n}개 저장됨", "INFO")
        except Exception as e:
            self._log(f"병렬 수 저장 실패: {e}", "ERROR")

    # ──────────────────────────────────────────────
    # 메뉴 핸들러
    # ──────────────────────────────────────────────

    def _on_manage_folders(self) -> None:
        """동기화 폴더 관리 대화상자 열기. YAML 편집기 필요 없음."""
        # 설정 파일이 없으면 템플릿부터 생성
        if not DEFAULT_CONFIG_PATH.exists():
            if messagebox.askyesno(
                "설정 파일 없음",
                "설정 파일이 없습니다. 기본 템플릿으로 생성할까요?\n\n"
                "(생성 후 폴더 관리 창이 열립니다)",
            ):
                save_config(default_config_template(), DEFAULT_CONFIG_PATH)
            else:
                return

        dlg = FolderManagerDialog(self.root, on_save=self._refresh_status)
        self.root.wait_window(dlg)

    def _on_edit_config(self) -> None:
        """기본 편집기로 config.yaml 열기."""
        if not DEFAULT_CONFIG_PATH.exists():
            if messagebox.askyesno(
                "설정 파일 없음",
                "설정 파일이 없습니다. 기본 템플릿으로 생성할까요?",
            ):
                save_config(default_config_template(), DEFAULT_CONFIG_PATH)
            else:
                return

        try:
            if sys.platform == "win32":
                os.startfile(str(DEFAULT_CONFIG_PATH))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(DEFAULT_CONFIG_PATH)])
            else:
                subprocess.Popen(["xdg-open", str(DEFAULT_CONFIG_PATH)])
            self._log(f"설정 파일 열림: {DEFAULT_CONFIG_PATH}", "INFO")
        except Exception as e:
            messagebox.showerror("오류", f"설정 파일을 열 수 없습니다:\n{e}")

    def _on_open_local_folder(self) -> None:
        """sync_pair의 로컬 폴더를 탐색기/Finder로 열기."""
        try:
            cfg = load_config()
        except FileNotFoundError:
            messagebox.showwarning("알림", "설정 파일이 없습니다.")
            return

        if not cfg.sync_pairs:
            messagebox.showinfo("알림", "동기화 쌍이 설정되지 않았습니다.")
            return

        target = cfg.sync_pairs[0].local_path
        target.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception as e:
            messagebox.showerror("오류", f"폴더를 열 수 없습니다:\n{e}")

    def _on_reset_state(self) -> None:
        if not messagebox.askyesno(
            "상태 초기화 확인",
            "동기화 상태를 초기화하면 다음 실행 시\n"
            "로컬과 리모트 전체를 비교합니다.\n\n"
            "계속하시겠습니까?",
        ):
            return
        try:
            cfg = load_config()
            for pair in cfg.sync_pairs:
                if clear_state(pair.local_path):
                    self._log(f"✓ 상태 삭제: {pair.local_path}", "SUCCESS")
                else:
                    self._log(f"  (없음)   {pair.local_path}", "DEBUG")
            self._refresh_status()
        except Exception as e:
            messagebox.showerror("오류", str(e))

    def _on_history(self) -> None:
        """동기화 히스토리 대화상자 열기."""
        dlg = HistoryDialog(self.root)
        self.root.wait_window(dlg)

    def _on_log_viewer(self) -> None:
        """전체 로그 뷰어 대화상자 열기."""
        dlg = LogViewerDialog(self.root)
        self.root.wait_window(dlg)

    def _on_normalize_filenames(self) -> None:
        """한글 파일명 NFD → NFC 일괄 정규화 대화상자 열기."""
        try:
            cfg = load_config()
        except FileNotFoundError:
            messagebox.showwarning("알림", "설정 파일이 없습니다.", parent=self.root)
            return
        if not cfg.sync_pairs:
            messagebox.showinfo("알림", "동기화 쌍이 설정되지 않았습니다.", parent=self.root)
            return
        dlg = NormalizeDialog(self.root, sync_pairs=cfg.sync_pairs, on_log=self._log)
        self.root.wait_window(dlg)

    def _on_bandwidth_editor(self) -> None:
        """대역폭 시간대 편집 대화상자 열기."""
        dlg = BandwidthEditorDialog(self.root, on_save=self._refresh_status)
        self.root.wait_window(dlg)

    def _on_scheduler(self) -> None:
        """예약 작업 관리 대화상자 열기."""
        dlg = SchedulerDialog(self.root)
        self.root.wait_window(dlg)

    def _on_single_sync(self) -> None:
        """개별 동기화: 여러 폴더 쌍을 선택해서 동기화."""
        self._open_subset_dialog(mode="sync")

    def _on_single_preview(self) -> None:
        """개별 미리보기: 여러 폴더 쌍을 선택해서 미리보기."""
        self._open_subset_dialog(mode="preview")

    def _open_subset_dialog(self, mode: str) -> None:
        try:
            cfg = load_config()
        except FileNotFoundError:
            messagebox.showwarning("알림", "설정 파일이 없습니다.", parent=self.root)
            return

        if not cfg.sync_pairs:
            messagebox.showinfo("알림", "동기화 쌍이 설정되지 않았습니다.", parent=self.root)
            return

        dlg = SingleSyncDialog(self.root, cfg.sync_pairs, mode=mode)
        self.root.wait_window(dlg)
        if not dlg.selected_indices:
            return

        if mode == "preview":
            # 미리보기는 옵션 다이얼로그 없이 바로 실행
            self._start_single_sync(dlg.selected_indices, dry_run=True)
            return

        # 개별 동기화: 옵션 다이얼로그 표시
        opt = SyncOptionsDialog(self.root, title="개별 동기화 옵션")
        self.root.wait_window(opt)
        if not opt.confirmed:
            return
        self._start_single_sync(
            dlg.selected_indices,
            dry_run=False,
            force_mode=opt.force_mode,
            shutdown_after=opt.shutdown_after,
        )

    def _start_single_sync(
        self,
        pair_indices: list[int],
        dry_run: bool = False,
        force_mode: Optional[str] = None,
        shutdown_after: bool = False,
    ) -> None:
        """선택한 동기화 쌍들을 실행."""
        with self._worker_lock:
            if self.worker_thread and self.worker_thread.is_alive():
                messagebox.showinfo("알림", "이미 작업이 실행 중입니다.")
                return

            cfg = load_config()
            pairs = [cfg.sync_pairs[i] for i in pair_indices]
            label = "미리보기" if dry_run else "동기화"
            mode_label = {
                "upload": " (업로드만)",
                "download": " (다운로드만)",
                None: "",
            }.get(force_mode, "")

            # PC 종료 예약 플래그 (체크박스도 같이 켜서 동기화 중 변경 가능하게)
            if shutdown_after and not dry_run:
                self._shutdown_after_sync = True
                try:
                    self.shutdown_var.set(True)
                except Exception:
                    pass

            if len(pairs) == 1:
                self._set_busy(True, f"개별 {label}{mode_label}: {pairs[0].local_path.name}")
                self._log_header(f"=== 개별 {label}{mode_label}: {pairs[0].local_path} ===")
            else:
                self._set_busy(True, f"개별 {label}{mode_label}: {len(pairs)}개 폴더")
                self._log_header(f"=== 개별 {label}{mode_label}: {len(pairs)}개 폴더 ===")

            self.worker_thread = threading.Thread(
                target=self._single_sync_worker,
                args=(pair_indices, dry_run, force_mode),
                daemon=True,
                name="gdrv-gui-single-sync",
            )
            self.worker_thread.start()

    def _single_sync_worker(
        self,
        pair_indices: list[int],
        dry_run: bool = False,
        force_mode: Optional[str] = None,
    ) -> None:
        """개별 동기화/미리보기 워커 (여러 폴더 쌍 처리)."""
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%H:%M:%S"))
        root_logger = logging.getLogger()
        prev_level = root_logger.level
        root_logger.setLevel(logging.INFO)
        root_logger.addHandler(handler)

        try:
            from gdrive_sync.drive_api import DriveClient
            from gdrive_sync.sync_engine import SyncEngine

            cfg = load_config()
            drive = DriveClient(
                cfg.network, interactive_auth=False,
                performance=cfg.performance,
                acknowledge_abuse=cfg.acknowledge_abuse,
            )
            limiter = make_limiter(cfg.bandwidth)

            def _progress_cb(completed, total, rel_path):
                self.log_queue.put(("progress", completed, f"{total}|{rel_path}"))

            def _status_cb(phase_text):
                self.log_queue.put(("status", phase_text, None))

            # 개별 동기화도 ProgressTracker 사용 — 선택된 pair만 분모로 잡기 위해
            # 임시로 cfg.sync_pairs 를 선택된 것만으로 좁힌 사본 사용
            from copy import copy as _copy
            tracker_cfg = _copy(cfg)
            tracker_cfg.sync_pairs = [cfg.sync_pairs[i] for i in pair_indices]
            self._progress_tracker = ProgressTracker(tracker_cfg)
            self._eta_calc = ETACalculator()

            engine = SyncEngine(
                cfg=cfg, drive=drive, dry_run=dry_run,
                force_mode=force_mode,
                progress_factory=None, bandwidth_limiter=limiter,
                progress_callback=_progress_cb,
                status_callback=_status_cb,
                progress_tracker=self._progress_tracker,
            )
            self.current_engine = engine

            # 선택된 폴더 쌍들만 순회
            selected_pairs = [cfg.sync_pairs[i] for i in pair_indices]

            total_up = 0
            total_dn = 0
            total_up_bytes = 0
            total_dn_bytes = 0
            total_errors = 0
            total_del_l = 0
            total_del_r = 0
            total_removed_state = 0
            all_summaries = []

            start = time.time()
            for pair in selected_pairs:
                if engine.is_stop_requested():
                    self.log_queue.put(("log", "WARNING",
                        f"중단 요청 — 남은 폴더 {len(selected_pairs) - len(all_summaries)}개 스킵"))
                    break
                self.log_queue.put(("log", "HEADER",
                    f"--- {pair.local_path.name or pair.local_path} ---"))
                summary = engine.sync_pair(pair)
                all_summaries.append((pair, summary))
                total_up += summary.uploaded
                total_dn += summary.downloaded
                total_up_bytes += summary.uploaded_bytes
                total_dn_bytes += summary.downloaded_bytes
                total_errors += summary.errors
                total_del_l += summary.deleted_local
                total_del_r += summary.deleted_remote
                total_removed_state += getattr(summary, "removed_state", 0)
            elapsed = time.time() - start

            if dry_run:
                # 미리보기: 결과를 다이얼로그로 보여주기
                self.log_queue.put(("log", "SUCCESS",
                    f"[DRY-RUN] {len(all_summaries)}개 폴더 미리보기 완료 ({elapsed:.1f}초)"))
                # UI 스레드에서 미리보기 결과 다이얼로그 열기
                self.root.after(
                    100,
                    lambda: self._show_preview_result(all_summaries, pair_indices),
                )
            else:
                self._real_sync_completed = True
                self.log_queue.put(("log", "SUCCESS",
                    f"완료: ↑{total_up} ↓{total_dn} "
                    f"오류:{total_errors} ({elapsed:.1f}초)"))
                try:
                    notify_sync_complete(total_up, total_dn, total_errors, elapsed)
                except Exception:
                    pass
                try:
                    single_action_summary: dict[str, int] = {}
                    for _, s in all_summaries:
                        for a in s.actions:
                            k = a.type.value
                            single_action_summary[k] = single_action_summary.get(k, 0) + 1
                    record = SyncRecord(
                        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        uploaded=total_up,
                        uploaded_bytes=total_up_bytes,
                        downloaded=total_dn,
                        downloaded_bytes=total_dn_bytes,
                        deleted=total_del_l + total_del_r,
                        removed_state=total_removed_state,
                        errors=total_errors,
                        elapsed_sec=elapsed,
                        pairs_count=len(all_summaries),
                        pair_paths=[str(p.local_path) for p, _ in all_summaries],
                        action_summary=single_action_summary,
                    )
                    append_record(record)
                except Exception:
                    pass

            self.log_queue.put(("done", None, None))
        except Exception as e:
            log.exception("개별 작업 실패")
            self.log_queue.put(("log", "ERROR", f"❌ 개별 작업 실패: {e}"))
            self.log_queue.put(("done", None, None))
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(prev_level)
            self.current_engine = None

    def _check_token_age(self) -> None:
        """시작 시 token.json 발급일 확인 — 6일+ 경과 시 상단 경고 배너 표시."""
        try:
            import time as _time
            if not DEFAULT_TOKEN_PATH.exists():
                return
            elapsed_days = (_time.time() - DEFAULT_TOKEN_PATH.stat().st_mtime) / 86400
            if elapsed_days < 6:
                return
            days = int(elapsed_days)
            msg = (f"⚠  토큰 발급 후 {days}일 경과 — 테스트 모드 앱이면 곧 만료될 수 있습니다. "
                   f"미리 재인증을 권장합니다.")
            self._show_token_banner(msg)
        except Exception:
            pass

    def _show_token_banner(self, msg: str) -> None:
        """상단에 노란 경고 배너 표시. 재인증 완료 후 _dismiss_token_banner()로 숨김."""
        if self._token_banner and self._token_banner.winfo_exists():
            return
        C = CLAUDE_COLORS
        banner = tk.Frame(self.root, bg="#FFF3CD", relief="flat", bd=0)
        banner.pack(side="top", fill="x", before=self.root.winfo_children()[0])

        tk.Label(
            banner, text=msg,
            bg="#FFF3CD", fg="#856404",
            font=("SF Pro Text", 11) if sys.platform == "darwin" else ("Segoe UI", 9),
            anchor="w", padx=12, pady=6,
        ).pack(side="left", fill="x", expand=True)

        def _reauth():
            self._dismiss_token_banner()
            self._on_auth()

        tk.Button(
            banner, text="재인증",
            bg="#007AFF", fg="white",
            relief="flat", bd=0, padx=10, pady=4,
            cursor="hand2",
            command=_reauth,
        ).pack(side="right", padx=(0, 8), pady=4)

        tk.Button(
            banner, text="✕",
            bg="#FFF3CD", fg="#856404",
            relief="flat", bd=0, padx=6,
            cursor="hand2",
            command=self._dismiss_token_banner,
        ).pack(side="right", pady=4)

        self._token_banner = banner

    def _dismiss_token_banner(self) -> None:
        if self._token_banner and self._token_banner.winfo_exists():
            self._token_banner.destroy()
        self._token_banner = None

    def _show_startup_summary(self) -> None:
        """프로그램 시작 시 직전 동기화 요약을 로그에 간단히 표시."""
        try:
            history = load_history()
            if not history:
                return
            last = history[-1]
            if last.dry_run:
                return
            from gdrive_sync.utils import format_duration
            from pathlib import Path as _P

            # 동기화한 폴더명 표시 (경로는 마지막 구성요소만, 3개까지 + 외 N개)
            names = [(_P(p).name or p) for p in last.pair_paths]
            if not names:
                folders_str = ""
            elif len(names) <= 3:
                folders_str = f"  [{', '.join(names)}]"
            else:
                folders_str = f"  [{', '.join(names[:3])} 외 {len(names) - 3}개]"

            self._log(
                f"[이전 동기화] {format_timestamp_local(last.timestamp)[:16]}  "
                f"↑{last.uploaded} ↓{last.downloaded} "
                f"오류:{last.errors}  ({format_duration(last.elapsed_sec)})"
                f"{folders_str}",
                "INFO",
            )
        except Exception:
            pass

    def _on_sync_finished(self) -> None:
        """동기화 완료 후 공통 후처리: 히스토리 자동 표시 + PC 종료.

        _poll_queue에서 done 메시지 받은 후 호출됨.
        """
        # 체크박스가 단일 소스 — 끝나는 순간의 상태로 결정
        # (동기화 중 사용자가 토글한 결과 반영)
        try:
            shutdown_requested = bool(self.shutdown_var.get())
        except Exception:
            shutdown_requested = self._shutdown_after_sync
        try:
            quit_requested = bool(self.quit_var.get())
        except Exception:
            quit_requested = self._quit_after_sync
        self._shutdown_after_sync = False
        self._quit_after_sync = False
        # 다음 동기화를 위해 체크박스 리셋 (세션 1회용)
        try:
            self.shutdown_var.set(False)
            self.quit_var.set(False)
        except Exception:
            pass

        if shutdown_requested:
            # PC 종료 예약 (프로그램 종료보다 우선 — PC가 꺼지면 프로그램도 함께 종료)
            self._schedule_shutdown()
            return   # 종료 예정이면 히스토리 안 띄움

        if quit_requested:
            # 프로그램(GUI) 종료 — 런처 applet까지 정리해 절전 어서션 해제
            self._quit_program()
            return   # 종료 예정이면 히스토리 안 띄움

        # 히스토리 자동 표시 (실제 전송이 있었을 때만)
        # dry-run이나 오류만 있었을 땐 안 띄움
        try:
            history = load_history()
            if history and not history[-1].dry_run and history[-1].errors == 0:
                # 1초 후 히스토리 다이얼로그 열기 (로그 메시지 보이도록)
                self.root.after(1000, lambda: HistoryDialog(self.root))
        except Exception:
            pass

    def _quit_program(self) -> None:
        """동기화 완료 후 프로그램(GUI)을 정상 종료한다.

        PC는 켜두되 프로그램만 닫아 절전이 정상 작동하게 한다.

        ★ 핵심: macOS에서 GUI를 띄운 런처(launch-gui.app/applet)가
        `PreventUserIdleSystemSleep` 어서션을 GUI 수명 내내 쥐고 있어,
        파이썬 GUI만 닫아선 맥북이 절전에 못 들어간다(배터리 방전 원인).
        따라서 종료 직전 caffeinate/Amphetamine 해제 + 런처 applet 정리까지
        수행해 모든 sleep 차단 요소를 확실히 제거한다.
        """
        self._log("🛑 동기화 완료 — 프로그램을 종료합니다 (절전 복원)", "WARNING")
        # 1) sleep 방지 요소 일괄 해제
        try:
            self._sleep_inhibitor.stop()
        except Exception:
            pass
        try:
            amphetamine_end_session()
        except Exception:
            pass
        # 2) macOS 런처 applet 종료 (어서션 보유 주체) — 파이썬과 독립 트리
        try:
            terminate_launcher_applet()
        except Exception:
            pass
        # 3) 창 위치 저장 후 GUI 종료
        try:
            self._save_window_geometry()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        # 4) 파이썬 프로세스 확실히 종료 (잔여 워커 스레드/타이머 무시하고 즉시)
        os._exit(0)

    def _start_shutdown_countdown(self, cancel_cmd: str) -> None:
        """상태바에 카운트다운 시작 + [취소] 버튼 노출 (60초)."""
        # 창이 최소화돼 있으면 복원 — 60초 카운트다운/취소 버튼을 볼 수 있게
        _ensure_viewable(self.root)
        self._shutdown_remaining = 60
        self._shutdown_cancel_cmd = cancel_cmd
        try:
            self._shutdown_cancel_btn.pack(side="right", padx=4, pady=2)
        except Exception:
            pass
        self._tick_shutdown_countdown()

    def _tick_shutdown_countdown(self) -> None:
        """1초마다 카운트다운 갱신."""
        if self._shutdown_remaining <= 0:
            # 카운트다운 끝 — 버튼 숨김 (실제 종료는 OS가 함)
            try:
                self._shutdown_cancel_btn.pack_forget()
            except Exception:
                pass
            self._shutdown_after_id = None
            return
        self.statusbar_var.set(f"⏰ {self._shutdown_remaining}초 후 PC 종료 — 취소: 우측 버튼")
        self._shutdown_remaining -= 1
        self._shutdown_after_id = self.root.after(1000, self._tick_shutdown_countdown)

    def _cancel_shutdown(self) -> None:
        """PC 종료 예약 취소."""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["shutdown", "/a"], capture_output=True, text=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
                )
            elif sys.platform == "darwin":
                subprocess.run(["killall", "shutdown"], capture_output=True, timeout=5)
            else:
                subprocess.run(["sudo", "shutdown", "-c"], capture_output=True, timeout=5)
            self._log("✓ PC 종료 취소됨", "SUCCESS")
        except Exception as e:
            self._log(f"⚠ 종료 취소 실패: {e} (직접 명령: {self._shutdown_cancel_cmd})", "ERROR")

        # 카운트다운 중지 + 버튼 숨김
        if self._shutdown_after_id:
            try:
                self.root.after_cancel(self._shutdown_after_id)
            except Exception:
                pass
            self._shutdown_after_id = None
        try:
            self._shutdown_cancel_btn.pack_forget()
        except Exception:
            pass
        self.statusbar_var.set("대기 중")

    def _schedule_shutdown(self) -> None:
        """동기화 완료 후 PC 종료 — 무인(unattended) 환경 자동 진행.

        ⚠ 핵심 변경: 추가 확인 다이얼로그 제거.
        사용자가 이미 체크박스로 명시 옵트인했으므로 즉시 시스템 종료 명령을 발행.
        퇴근 후 무인 자동 종료가 정상 작동하도록 보장.

        취소 방법: 상태바 [PC 종료 취소] 버튼 (60초 카운트다운 동안) 또는
        - Windows: 명령 프롬프트에서 `shutdown /a`
        - macOS:   터미널에서 `sudo killall shutdown`
        - Linux:   터미널에서 `sudo shutdown -c`

        Windows: `shutdown /s /t 60`이 시스템 알림(트레이)을 자동으로 띄움.
        macOS:   sudo 권한 있으면 `sudo shutdown -h +1` 우선 시도, 아니면 osascript fallback.
        """
        cancel_cmd = {
            "win32": "shutdown /a",
            "darwin": "sudo killall shutdown",
        }.get(sys.platform, "sudo shutdown -c")

        try:
            if sys.platform == "win32":
                subprocess.Popen(
                    ["shutdown", "/s", "/t", "60", "/c", "gdrive-sync 동기화 완료"],
                    creationflags=(
                        subprocess.CREATE_NO_WINDOW
                        if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
                    ),
                )
                self._start_shutdown_countdown(cancel_cmd)
                self._log(
                    "⏰ 60초 후 PC 종료 예약 — 상태바 [취소] 버튼 또는 'shutdown /a'",
                    "WARNING",
                )

            elif sys.platform == "darwin":
                # 1차: sudo -n (비밀번호 없이) shutdown 시도
                try:
                    result = subprocess.run(
                        ["sudo", "-n", "shutdown", "-h", "+1"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result.returncode == 0:
                        self._start_shutdown_countdown(cancel_cmd)
                        self._log("✓ Mac 1분 후 자동 종료 예약됨 (sudo 성공)", "INFO")
                        return
                except Exception:
                    pass
                # 2차: osascript (시스템 종료 다이얼로그 뜸 — 무인 환경 한계)
                subprocess.Popen([
                    "osascript", "-e",
                    'tell application "System Events" to shut down',
                ])
                self._log(
                    "⚠ Mac sudo 권한 없음 — osascript로 종료 요청 (시스템 다이얼로그 뜰 수 있음).",
                    "WARNING",
                )

            else:
                # Linux
                result = subprocess.run(
                    ["shutdown", "-h", "+1"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    result2 = subprocess.run(
                        ["sudo", "-n", "shutdown", "-h", "+1"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if result2.returncode != 0:
                        self._log(
                            f"⚠ shutdown 실행 실패 (sudo 필요): {result.stderr.strip()}",
                            "ERROR",
                        )
                        return
                self._start_shutdown_countdown(cancel_cmd)
                self._log("⏰ 60초 후 Linux 종료 예약됨", "WARNING")

        except Exception as e:
            self._log(f"⚠ PC 종료 명령 실행 실패: {e}", "ERROR")

    def _show_preview_result(self, all_summaries, pair_indices: list[int]) -> None:
        """미리보기 완료 후 결과 다이얼로그를 띄우고, 사용자가 OK하면 실제 동기화 실행."""
        try:
            cfg = load_config()
        except Exception:
            return

        dlg = PreviewResultDialog(self.root, all_summaries, cfg)
        self.root.wait_window(dlg)

        if dlg.proceed_with_sync:
            # 그대로 동기화 진행
            if dlg.shutdown_after:
                self._shutdown_after_sync = True
            # 전체 sync_pairs이면 기본 동기화, 일부면 개별 동기화
            if len(pair_indices) == len(cfg.sync_pairs):
                self._start_sync(dry_run=False)
            else:
                self._start_single_sync(pair_indices, dry_run=False)

    def _on_about(self) -> None:
        messagebox.showinfo(
            "gdrive-sync 정보",
            f"gdrive-sync {__version__}\n\n"
            f"크로스플랫폼 Google Drive 양방향 동기화 CLI\n\n"
            f"Python: {sys.version.split()[0]}\n"
            f"Platform: {sys.platform}\n"
            f"설정: {DEFAULT_CONFIG_PATH}",
        )

    def _on_create_shortcut(self) -> None:
        """도구 메뉴 → 바탕화면 바로가기 만들기 (Windows 전용)."""
        try:
            from gdrive_sync.context_menu import create_desktop_shortcut
            path = create_desktop_shortcut()
            self.log_queue.put(("log", "SUCCESS", f"✓ 바탕화면 바로가기 생성: {path}"))
            messagebox.showinfo("바로가기", f"바탕화면에 만들었습니다:\n{path}")
        except Exception as e:
            self.log_queue.put(("log", "ERROR", f"❌ 바로가기 생성 실패: {e}"))
            messagebox.showwarning("바로가기", f"생성 실패: {e}")

    # ──────────────────────────────────────────────
    # 새 버전 확인 / 업데이트 (v2.4)
    # ──────────────────────────────────────────────

    def _on_check_update(self) -> None:
        """도움말 메뉴 → 업데이트 확인 (수동, 스로틀 무시)."""
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        if self._update_check_running:
            return
        self._update_check_running = True
        threading.Thread(
            target=self._update_check_worker, args=(manual,), daemon=True,
        ).start()

    def _update_check_worker(self, manual: bool) -> None:
        """백그라운드 스레드: 네트워크 조회만 하고 결과는 UI 스레드로 전달."""
        info = None
        try:
            from gdrive_sync.update_check import check_for_update
            info = check_for_update(force=manual, timeout=8.0 if manual else 4.0)
        except Exception as e:
            log.debug(f"업데이트 확인 실패 (무시): {e}")
        try:
            self.root.after(0, lambda: self._on_update_check_done(info, manual))
        except RuntimeError:
            pass  # 창이 이미 닫힘

    def _on_update_check_done(self, info, manual: bool) -> None:
        self._update_check_running = False

        if info is None:
            if manual:
                messagebox.showwarning(
                    "업데이트 확인",
                    "버전 정보를 가져오지 못했습니다.\n"
                    "네트워크 또는 저장소 접근 권한을 확인하세요.",
                )
            return

        if not info.available:
            if manual:
                messagebox.showinfo("업데이트 확인", f"이미 최신 버전입니다 (v{info.current}).")
            return

        # 새 버전 있음
        self.log_queue.put(("log", "WARNING",
            f"🔔 새 버전 v{info.latest} 발견 (현재 v{info.current}) — "
            f"도움말 메뉴 → 업데이트 확인"))
        if not manual:
            # 자동 확인: 토스트+로그만 — 시작하자마자 모달로 방해하지 않음
            notify("gdrive-sync 새 버전",
                   f"v{info.latest} 이(가) 나왔습니다 (현재 v{info.current})")
            return
        if messagebox.askyesno(
            "업데이트", f"새 버전 v{info.latest} 이(가) 있습니다 (현재 v{info.current}).\n"
                       f"지금 업데이트할까요? (완료 후 프로그램 재시작 필요)"):
            self._run_upgrade_async(info)

    def _run_upgrade_async(self, info) -> None:
        self.log_queue.put(("log", "INFO", f"⬇ v{info.latest} 업데이트 설치 중... (pip)"))

        def worker():
            try:
                from gdrive_sync.update_check import run_pip_upgrade
                rc, output = run_pip_upgrade(quiet=True)
            except Exception as e:
                rc, output = 1, str(e)
            if rc == 0:
                self.log_queue.put(("log", "SUCCESS",
                    f"✓ v{info.latest} 업데이트 완료 — 프로그램을 재시작하면 적용됩니다."))
                notify("gdrive-sync 업데이트 완료",
                       f"v{info.latest} 설치됨. 재시작하면 적용됩니다.")
            else:
                tail = "\n".join(output.strip().splitlines()[-5:])
                self.log_queue.put(("log", "ERROR",
                    f"❌ 업데이트 실패 (pip 종료코드 {rc})\n{tail}"))

        threading.Thread(target=worker, daemon=True).start()

    # ──────────────────────────────────────────────
    # 로그 출력 유틸
    # ──────────────────────────────────────────────

    # 로그 위젯 성능 상수
    _MAX_LOG_LINES = 3000       # 위젯에 유지할 최대 줄 수
    _MAX_POLL_BATCH = 100       # 한 번의 poll 사이클에서 처리할 최대 메시지 수

    def _poll_queue(self) -> None:
        """워커 스레드가 큐에 넣은 메시지를 UI에 반영.

        메시지 타입:
        - ("log", level, msg): 로그 패널에 삽입
        - ("progress", completed_int, total_int): 프로그래스 바 업데이트
        - ("done", None, None): 작업 완료
        """
        batch: list[tuple[str, str]] = []
        done_received = False
        last_progress = None  # (completed, total) — 마지막 것만 UI 업데이트
        last_status = None    # 마지막 상태(단계) 텍스트만 UI 반영
        count = 0

        try:
            while count < self._MAX_POLL_BATCH:
                kind, level, msg = self.log_queue.get_nowait()
                count += 1
                if kind == "log":
                    batch.append((level or "INFO", msg))
                elif kind == "progress":
                    last_progress = (level, msg)  # level=completed, msg=total
                elif kind == "status":
                    last_status = level  # level=phase_text
                elif kind == "auth_ok":
                    self._dismiss_token_banner()
                elif kind == "done":
                    done_received = True
        except queue.Empty:
            pass

        # 워치독: 메시지가 하나라도 들어왔으면 활동 시각 갱신
        if count > 0:
            self._last_activity_ts = time.monotonic()

        if last_status is not None:
            self._current_phase = str(last_status)
            # 경과 시간과 함께 상태바 즉시 갱신
            self._update_elapsed_label()

        if batch:
            self._log_batch(batch)

        if last_progress:
            completed_val, total_and_path = last_progress
            # total_and_path 형식: "575|파일명.pdf" 또는 "575" (구버전 호환)
            total_str = str(total_and_path)
            rel_path = ""
            if "|" in total_str:
                total_str, rel_path = total_str.split("|", 1)
            self._update_progress(int(completed_val), int(total_str), rel_path)

        if done_received:
            self._hide_progress()
            self._set_busy(False, "완료")
            self._refresh_status()
            # 실제 동기화 완료 시에만 후처리 (미리보기/연결테스트/인증 시엔 안 뜸)
            if self._real_sync_completed:
                self._real_sync_completed = False
                self.root.after(300, self._on_sync_finished)

        # 동기화 진행 중이면 매 폴링마다 전체 진행 + ETA 갱신
        # (progress_callback 없는 단계 — 스캔/폴더생성 중 — 에서도 시각 갱신)
        if self._progress_inner.winfo_ismapped() and getattr(self, "_progress_tracker", None):
            try:
                self._update_overall_progress()
            except Exception:
                pass

        # ── 모달 grab 데드락 안전망 ──
        # 모달 자식이 grab 을 잡은 채 메인 창이 최소화/숨김이면, 보이지 않는
        # 모달이 입력을 가로채 창이 영영 안 나온다. 매 폴링마다 점검해 자동 복원.
        # (after 타이머는 grab 중에도 계속 돌므로 이 검사가 동작함.)
        try:
            if self.root.grab_current() is not None and \
                    self.root.state() in ("iconic", "withdrawn"):
                self.root.deiconify()
                self.root.lift()
        except Exception:
            pass

        # ── 무진행 워치독 ──
        # "동기화 완료 후 PC 종료"가 켜진 상태에서만 발동.
        # 워커 큐가 NO_PROGRESS_THRESHOLD_SEC 동안 한 줄도 안 보내면
        # 워커가 정지한 것으로 간주 → 강제 종료 + shutdown 직접 트리거.
        self._check_no_progress_watchdog()

        self.root.after(self.QUEUE_POLL_MS, self._poll_queue)

    def _check_no_progress_watchdog(self) -> None:
        """무진행 워치독 평가. _poll_queue에서 매 사이클 호출."""
        if self._watchdog_triggered:
            return
        if self._last_activity_ts is None:
            return
        # busy 상태인지 — sync 버튼이 disabled면 작업 진행 중
        try:
            if str(self.sync_btn.cget("state")) != "disabled":
                return
        except Exception:
            return

        now = time.monotonic()
        reason: Optional[str] = None

        # (A) 큐 침묵 — 워커 스레드가 완전히 멈춘 케이스
        elapsed = now - self._last_activity_ts
        if elapsed >= self.NO_PROGRESS_THRESHOLD_SEC:
            mins = int(elapsed // 60)
            reason = f"{mins}분간 진행 신호 없음"

        # (B) 바이트 트리클 — 메시지는 가끔 오지만 실제 전송이 거의 멈춘 케이스
        if reason is None:
            stall = self._check_bytes_stall(now)
            if stall:
                reason = stall

        if reason is None:
            return

        # 발동 — 1회만
        self._watchdog_triggered = True
        try:
            shutdown_on = bool(self.shutdown_var.get())
        except Exception:
            shutdown_on = False
        followup = "PC 종료 절차로 진입합니다." if shutdown_on else "동기화를 중단합니다."
        self._log(
            f"⚠ 무진행 워치독 발동 — {reason}. "
            f"워커를 강제 종료하고 {followup}",
            "ERROR",
        )
        engine = self.current_engine
        if engine is not None:
            try:
                engine.request_force_stop()
            except Exception as e:
                self._log(f"강제 종료 호출 실패(무시): {e}", "WARNING")
        # 워커가 done 안 보내도 진행 — 5초 후 sync 완료 핸들러 직접 호출
        self.root.after(5000, self._watchdog_finalize)

    def _check_bytes_stall(self, now: float) -> Optional[str]:
        """바이트-진척 워치독 평가. 정체 시 사유 문자열 반환, 아니면 None.

        - bytes_done 이 BYTES_STALL_MIN_DELTA 이상 진척하면 베이스라인 리셋
        - 그 외엔 베이스라인 이후 경과시간이 임계 초과 시 발동
        - bytes_done == 0 이면(전송 시작 전: 스캔/폴더생성 단계) 미발동
        """
        tracker = getattr(self, "_progress_tracker", None)
        if tracker is None:
            return None
        try:
            snap = tracker.snapshot()
        except Exception:
            return None
        bytes_now = int(getattr(snap, "bytes_done", 0) or 0)
        if bytes_now <= 0:
            # 아직 파일 전송 단계 아님 — 베이스라인 미설정 상태 유지
            return None
        if self._bytes_baseline_ts is None:
            self._bytes_baseline = bytes_now
            self._bytes_baseline_ts = now
            return None
        if bytes_now - self._bytes_baseline >= self.BYTES_STALL_MIN_DELTA:
            # 의미 있는 진척 — 베이스라인 갱신
            self._bytes_baseline = bytes_now
            self._bytes_baseline_ts = now
            return None
        elapsed = now - self._bytes_baseline_ts
        if elapsed < self.BYTES_STALL_THRESHOLD_SEC:
            return None
        delta_kb = (bytes_now - self._bytes_baseline) / 1024
        mins = int(elapsed // 60)
        return f"{mins}분간 바이트 진척 {delta_kb:.0f} KB (트리클)"

    def _watchdog_finalize(self) -> None:
        """워치독 발동 후 done 시그널 없이도 종료 절차 진행."""
        # 워커가 그 사이에 정상 종료했으면 busy=False 상태일 것 — 그러면 skip
        try:
            if str(self.sync_btn.cget("state")) != "disabled":
                return
        except Exception:
            return
        # busy 상태 해제 + shutdown 트리거
        self._hide_progress()
        self._set_busy(False, "워치독 강제 종료")
        self._real_sync_completed = False  # 정상 완료 아니므로 히스토리 미오픈
        try:
            if self.shutdown_var.get():
                self._schedule_shutdown()
        except Exception:
            pass

    # ──────────────────────────────────────────────
    # 프로그래스 바 표시/갱신/숨김
    # ──────────────────────────────────────────────

    def _show_progress(self, total: int) -> None:
        """진행 상황 프레임 표시 + 초기화."""
        self._progress_bar["maximum"] = max(total, 1)
        self._progress_bar["value"] = 0
        self._progress_label.set(f"[현재 폴더] 0 / {total} (0%)")
        self._overall_bar["maximum"] = 100
        self._overall_bar["value"] = 0
        self._overall_label.set("[전체] 시작 중...")
        self._stats_label.set("")
        self._eta_label.set("ETA: 측정 중...")
        self._last_progress_at = time.time()
        self._progress_inner.pack(fill="x", pady=(0, 6))
        # (C) 패널이 붙으면서 창이 자연 크기로 늘어나지 않도록 현재 크기 고정
        self._lock_geometry_after_pack()

    def _update_progress(self, completed: int, total: int, rel_path: str = "") -> None:
        """진행 상황 업데이트 (poll 사이클당 1회)."""
        if not self._progress_inner.winfo_ismapped():
            self._show_progress(total)

        # 마지막 progress 갱신 시각 기록 (멈춤 감지에 사용)
        self._last_progress_at = time.time()

        self._progress_bar["maximum"] = max(total, 1)
        self._progress_bar["value"] = completed
        pct = (completed / total * 100) if total else 0

        # [현재 폴더] 라벨: 파일명 포함
        if rel_path:
            short = rel_path.rsplit("/", 1)[-1] if "/" in rel_path else rel_path
            if len(short) > 45:
                short = "..." + short[-42:]
            self._progress_label.set(f"[현재 폴더] {completed} / {total} ({pct:.0f}%) — {short}")
        else:
            self._progress_label.set(f"[현재 폴더] {completed} / {total} ({pct:.0f}%)")

        # 전체 진행 + 통계 + ETA 갱신
        self._update_overall_progress()

        # 전송 단계에서는 상태바에도 X/Y 반영 (엔진 단계 텍스트 뒤에 덧붙임)
        if self._sync_start_ts is not None:
            base = self._current_phase or "↑↓ 파일 전송 중"
            # 이미 "· N개" 같은 꼬리가 있으면 제거하고 X/Y로 교체
            base = base.split(" · ")[0] if " · " in base else base
            self._current_phase = f"{base} · {completed}/{total} ({pct:.0f}%)"
            self._update_elapsed_label()

    def _check_stuck_progress(self) -> str:
        """현재 파일의 progress가 N초 이상 안 갱신되면 멈춤 안내 문구 반환.

        단일 청크 모드는 사전 throttle 대기 중에도 progress_cb 가 1초마다 호출되므로,
        15초 이상 정체는 네트워크 지연이나 큰 파일 PUT 진행 중 등 다른 원인.
        """
        last = getattr(self, "_last_progress_at", None)
        if last is None:
            return ""
        elapsed = time.time() - last
        if elapsed < 15:
            return ""
        if elapsed < 60:
            return f"   ⚠ {int(elapsed)}초간 진행 표시 없음 (네트워크 지연 또는 큰 파일 PUT 중)"
        m = int(elapsed // 60)
        s = int(elapsed % 60)
        return f"   ⚠ {m}분 {s}초간 진행 표시 없음"

    def _update_overall_progress(self) -> None:
        """ProgressTracker + ETA 기반으로 전체/통계/ETA 라벨 갱신."""
        tracker = getattr(self, "_progress_tracker", None)
        eta = getattr(self, "_eta_calc", None)
        if not tracker or not eta:
            return
        try:
            snap = tracker.snapshot()
        except Exception:
            return

        # 전체 진행률
        ratio = snap.overall_ratio
        self._overall_bar["value"] = int(ratio * 100)

        pair_info = ""
        if snap.current_pair_name:
            pair_info = f" — 현재: {snap.current_pair_name}"
        self._overall_label.set(
            f"[전체] {snap.label} 폴더 {snap.pairs_done}/{snap.pairs_total} "
            f"+ 파일 {snap.files_done:,}/{snap.files_total:,} ({ratio*100:.1f}%){pair_info}"
        )

        # 통계 (바이트 + 속도)
        eta.update(snap.bytes_done)
        bytes_done_mb = snap.bytes_done / 1024 / 1024
        bytes_total_mb = snap.bytes_total / 1024 / 1024
        rate_text = eta.format_rate()
        self._stats_label.set(
            f"데이터: {bytes_done_mb:.1f} MB / {bytes_total_mb:.1f} MB    속도: {rate_text}"
        )

        # ETA + 멈춤 감지 안내
        remaining_bytes = max(0, snap.bytes_total - snap.bytes_done)
        eta_text = eta.format(remaining_bytes)
        stuck = self._check_stuck_progress()
        if stuck:
            self._eta_label.set(f"ETA: {eta_text}{stuck}")
        else:
            self._eta_label.set(f"ETA: {eta_text}")

    def _hide_progress(self) -> None:
        """진행 상황 프레임 숨김."""
        self._progress_inner.pack_forget()
        # (C) 패널이 떨어지면서 창이 자연 크기로 줄어들지 않도록 현재 크기 고정
        self._lock_geometry_after_pack()

    def _lock_geometry_after_pack(self) -> None:
        """(C) 동적 pack/pack_forget 직후 사용자 창 크기를 보존.

        진행 패널의 추가/제거로 Tk 지오메트리 매니저가 창을 콘텐츠 자연 크기로
        재계산하는 것을 막는다. 정상 상태의 유효 geometry 일 때만 재적용.
        """
        if not getattr(self, "_geo_settled", False):
            return
        try:
            if self.root.state() != "normal":
                return   # 최소화/withdrawn 중엔 garbage — 건드리지 않음
            geo = self.root.geometry()
        except Exception:
            return
        parsed = self._parse_geo(geo)
        if not parsed:
            return
        w, h, x, y = parsed
        if not self._geo_size_sane(w, h) or x < -50 or y < -50:
            return
        # update_idletasks 후 자연 크기 재계산이 끝난 시점에 다시 사용자 크기 적용
        self.root.after_idle(lambda g=geo: self._reapply_geo(g))

    def _log_batch(self, entries: list[tuple[str, str]]) -> None:
        """여러 로그 메시지를 한 번의 위젯 조작으로 삽입."""
        self.log_text.config(state="normal")

        for level, msg in entries:
            self.log_text.insert("end", msg + "\n", level)

        # 줄 수 제한 (오래된 줄 삭제)
        total_lines = int(self.log_text.index("end-1c").split(".")[0])
        if total_lines > self._MAX_LOG_LINES:
            excess = total_lines - self._MAX_LOG_LINES
            self.log_text.delete("1.0", f"{excess + 1}.0")

        # 스크롤은 배치당 1회만
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _log(self, msg: str, level: str = "INFO") -> None:
        """단일 메시지 삽입 (배치 밖에서 직접 호출용)."""
        self._log_batch([(level, msg)])

    def _log_header(self, msg: str) -> None:
        self._log("", "HEADER")
        self._log(msg, "HEADER")

    def _clear_log(self) -> None:
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    def _set_busy(self, busy: bool, status_msg: str = "") -> None:
        self.statusbar_var.set(status_msg or "대기 중")
        new_state = "disabled" if busy else "normal"
        self.sync_btn.config(state=new_state)
        self.dryrun_btn.config(state=new_state)
        self.test_btn.config(state=new_state)
        self.single_sync_btn.config(state=new_state)
        self.single_preview_btn.config(state=new_state)
        # 3단계 중단 버튼 모두 활성/비활성
        stop_state = "normal" if busy else "disabled"
        self.stop_after_pair_btn.config(state=stop_state)
        self.stop_after_file_btn.config(state=stop_state)
        self.force_stop_btn.config(state=stop_state)
        # 커서 변경
        try:
            self.root.config(cursor="watch" if busy else "")
        except tk.TclError:
            pass
        # 경과 시간 티커: 시작은 status_msg 있을 때만, 종료는 항상
        if busy:
            self._current_phase = status_msg or "동기화 중"
            self._start_elapsed_ticker()
            # 워치독 상태 리셋 — 활동 타이머 기동
            self._last_activity_ts = time.monotonic()
            self._watchdog_triggered = False
            # 바이트 베이스라인은 _check_bytes_stall이 첫 진척 시 자동 설정
            self._bytes_baseline_ts = None
            self._bytes_baseline = 0
            # 동기화 중 시스템 sleep 방지 (Mac/Linux/Windows 자동 분기)
            try:
                if self._sleep_inhibitor.start():
                    self._log("💤 시스템 sleep 방지 활성화", "INFO")
            except Exception:
                pass
            # Amphetamine 세션 시작 (설치되어 있을 때만, 뚜껑 닫힌 배터리 환경 대비)
            try:
                if amphetamine_start_session():
                    self._log(
                        "☕ Amphetamine 세션 시작 — 동기화 완료 후 자동 종료됩니다\n"
                        "   ※ Amphetamine의 '앱 실행 중' 트리거는 제거를 권장합니다\n"
                        "     (GUI가 직접 세션을 제어하므로 트리거 불필요)",
                        "INFO",
                    )
            except Exception:
                pass
        else:
            self._stop_elapsed_ticker()
            # 동기화 종료 → caffeinate 해제
            try:
                if self._sleep_inhibitor.is_active():
                    self._sleep_inhibitor.stop()
                    self._log("💤 caffeinate 해제", "INFO")
            except Exception:
                pass
            # Amphetamine 세션 종료 + 뚜껑 상태 로그
            self.root.after(200, self._release_amphetamine_and_log_lid)

    def _release_amphetamine_and_log_lid(self) -> None:
        """동기화 완료 후 Amphetamine 세션 종료 + 뚜껑 상태에 따른 안내 로그.

        _set_busy(False) 직후 root.after(200, ...) 로 호출됩니다.

        뚜껑 닫힘 → Amphetamine 세션 종료 → macOS 정상 절전
        뚜껑 열림  → Amphetamine 세션 종료 → 화면보호기/디스플레이 절전 복원
        Amphetamine 미설치 → 아무것도 안 함 (caffeinate만으로 충분)
        """
        import sys
        if sys.platform != "darwin":
            return

        try:
            ended = amphetamine_end_session()
            lid_closed = mac_lid_is_closed()

            if ended:
                if lid_closed:
                    self._log(
                        "☕🔋 Amphetamine 세션 종료 — 뚜껑이 닫혀 있으므로 맥북이 정상 절전됩니다",
                        "SUCCESS",
                    )
                else:
                    self._log(
                        "☕ Amphetamine 세션 종료 — 화면보호기 / 전력 관리가 평소대로 복원됩니다",
                        "INFO",
                    )
            else:
                # Amphetamine 미설치 or 이미 세션 없음 → caffeinate로만 처리됐음
                if lid_closed:
                    self._log(
                        "🔋 뚜껑이 닫혀 있습니다 — caffeinate 해제됐으므로 맥북이 절전됩니다\n"
                        "   (Amphetamine 미설치 또는 세션 없음)",
                        "INFO",
                    )
        except Exception as e:
            log.debug(f"_release_amphetamine_and_log_lid 오류: {e}")

    # ──────────────────────────────────────────────
    # 경과 시간 티커 (1초 간격 상태바 갱신)
    # ──────────────────────────────────────────────

    def _start_elapsed_ticker(self) -> None:
        """작업 시작. 1초마다 '{단계} — 경과 mm:ss' 로 상태바 갱신."""
        self._sync_start_ts = time.monotonic()
        self._schedule_elapsed_tick()

    def _stop_elapsed_ticker(self) -> None:
        """작업 종료. 티커 정지."""
        self._sync_start_ts = None
        if self._elapsed_after_id is not None:
            try:
                self.root.after_cancel(self._elapsed_after_id)
            except Exception:
                pass
            self._elapsed_after_id = None
        self._current_phase = ""

    def _schedule_elapsed_tick(self) -> None:
        self._update_elapsed_label()
        if self._sync_start_ts is not None:
            self._elapsed_after_id = self.root.after(
                1000, self._schedule_elapsed_tick
            )

    def _update_elapsed_label(self) -> None:
        """현재 단계 + 경과 시간을 상태바에 표시."""
        if self._sync_start_ts is None:
            return
        elapsed = int(time.monotonic() - self._sync_start_ts)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
        phase = self._current_phase or "동기화 중"
        self.statusbar_var.set(f"{phase} — 경과 {time_str}")

    # ──────────────────────────────────────────────
    # 종료
    # ──────────────────────────────────────────────

    # ──────────────────────────────────────────────
    # 창 위치/크기 기억
    # ──────────────────────────────────────────────

    # 스타트업 settle 기간 — pywinstyles 자동 재배치 + 비동기 위젯 빌드가 끝날
    # 시간을 줘서 그 사이 자동 발생하는 Configure → save 로 사용자의 마지막
    # 위치를 덮어쓰지 않도록 함. 윈도우는 withdraw 로 숨겨져 있다가 이 시간이
    # 끝나면 deiconify 로 한 번에 표시되므로 사용자에겐 깜빡임 없음.
    # 이 시간이 너무 짧으면 자동 재배치를 못 막고, 너무 길면 GUI 가 늦게 뜸.
    _GEO_SETTLE_MS = 400

    def _restore_window_geometry(self) -> None:
        """이전 세션의 창 위치/크기를 gui_state.json에서 복원.

        파일이 없거나 모니터 밖이면 기본 geometry(920x680) 적용.

        ⚠ 스타트업 settle: 복원 직후 1.5초간은 _save_window_geometry 가
        들어와도 무시한다. pywinstyles.apply_style 가 위젯 위치를 임의
        좌표(+52+52 등)로 자동 이동시키는 부작용 + 비동기 위젯 빌드 + 토큰
        배너 등이 Configure 를 트리거 → save 가 잘못된 위치를 잡아채는
        버그 방지. settle 종료 시점에 원래 의도된 geometry 를 다시 한 번
        강제 적용해 자동 이동을 되돌린다.
        """
        import json
        default_geo = "960x700"
        # 원래 복원하려던 geometry — settle 끝에 재적용용
        self._restored_geo: Optional[str] = None
        try:
            if self._GUI_STATE_PATH.exists():
                with open(self._GUI_STATE_PATH, "r", encoding="utf-8") as f:
                    state = json.load(f)
                geo = state.get("geometry", default_geo)
                expanded = state.get("status_expanded", False)

                # geometry 문자열에서 직접 W, H, X, Y 파싱 (winfo_* 는 렌더링 전 엉뚱한 값 반환)
                parsed = self._parse_geo(geo)
                if parsed:
                    w, h, x, y = parsed
                    sw = self.root.winfo_screenwidth()
                    sh = self.root.winfo_screenheight()
                    # 타이틀바가 화면 밖으로 나가거나(위치), 크기가 비정상이면(D) 기본값
                    if x < -50 or y < -50 or x > sw - 100 or y > sh - 100:
                        geo = default_geo
                    elif not self._geo_size_sane(w, h):
                        # 위치는 유지하되 크기만 기본값으로 교정
                        geo = f"{self._parse_geo(default_geo)[0]}x{self._parse_geo(default_geo)[1]}+{x}+{y}"
                else:
                    geo = default_geo
                self.root.geometry(geo)
                self._restored_geo = geo

                # 토글 상태 복원
                self._status_expanded = expanded
                if expanded:
                    self._toggle_btn_text.set("▲ 접기")
                # settle 타이머 등록 — 그 전까지는 모든 save 무시
                self._geo_settled = False
                self.root.after(self._GEO_SETTLE_MS, self._mark_geo_settled)
                return
        except Exception:
            pass
        self.root.geometry(default_geo)
        self._restored_geo = default_geo
        self._geo_settled = False
        self.root.after(self._GEO_SETTLE_MS, self._mark_geo_settled)

    def _mark_geo_settled(self) -> None:
        """settle 기간 종료.

        그동안 pywinstyles 등 외부 코드가 윈도우를 의도와 다른 위치로 이동시켰을
        수 있으므로 원래 복원했던 geometry 를 한 번 더 적용해 강제로 되돌리고,
        withdraw 로 숨겨뒀던 윈도우를 한 번에 표시한다. save 게이트도 연다.
        사용자에겐 깜빡임 없이 마지막 위치에 바로 뜨는 것처럼 보임.
        """
        target = getattr(self, "_restored_geo", None)
        if target:
            try:
                self.root.geometry(target)   # 항상 한 번 더 적용 — 자동 이동 캔슬
                self.root.update_idletasks()  # 적용 강제 반영
            except Exception:
                pass
        try:
            self.root.deiconify()   # 이제 사용자에게 보여줌
        except Exception:
            pass
        # 복원된 geometry 를 최소화→복원 재적용용 기준값으로 시드
        self._last_normal_geo = target
        # (E) deiconify 직후 실측으로 화면 밖이면 중앙으로 끌어옴
        self.root.after_idle(self._recenter_if_offscreen)
        # deiconify 가 트리거하는 Configure → save 가 다시 잘못된 값을 잡지
        # 않도록 짧은 추가 가드 후 게이트 오픈
        self.root.after(150, self._open_geo_save_gate)

    def _open_geo_save_gate(self) -> None:
        """save 게이트 오픈 — 이후 Configure 는 사용자 의도로 간주."""
        self._geo_settled = True

    # ── geometry 파싱/검증 헬퍼 ──────────────────────────────

    @staticmethod
    def _parse_geo(geo: str):
        """'960x700+100+200' → (w, h, x, y). 형식 불일치 시 None."""
        import re
        m = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", geo or "")
        if not m:
            return None
        return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))

    def _geo_size_sane(self, w: int, h: int) -> bool:
        """크기가 minsize 이상이고 화면을 크게 벗어나지 않으면 True (D)."""
        try:
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            sw, sh = 10_000, 10_000
        if w < self._MIN_W or h < self._MIN_H:
            return False
        # 멀티모니터 가상 폭까지 고려해 넉넉히 (주 모니터의 3배까지 허용)
        if w > sw * 3 or h > sh * 3:
            return False
        return True

    def _recenter_if_offscreen(self) -> None:
        """(E) 실제 렌더링된 창이 화면 밖이면 주 모니터 중앙으로 이동."""
        try:
            self.root.update_idletasks()
            x = self.root.winfo_x()
            y = self.root.winfo_y()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
        except Exception:
            return
        # 타이틀바가 화면 안에 최소한 걸쳐 있는지 (한 변이라도 완전히 밖이면 교정)
        offscreen = (
            x + w < 50 or y + h < 50 or
            x > sw - 50 or y > sh - 50 or
            y < -50
        )
        if not offscreen:
            return
        nw = min(max(w, self._MIN_W), sw)
        nh = min(max(h, self._MIN_H), sh)
        nx = max(0, (sw - nw) // 2)
        ny = max(0, (sh - nh) // 3)
        try:
            self.root.geometry(f"{nw}x{nh}+{nx}+{ny}")
            self._last_normal_geo = self.root.geometry()
        except Exception:
            pass

    def _ensure_visible(self) -> None:
        """(A) deiconify 가 실패해 창이 withdrawn 으로 남았으면 강제 표시."""
        try:
            st = self.root.state()
        except Exception:
            return
        if st == "withdrawn":
            try:
                self.root.deiconify()
                self.root.lift()
                self._geo_settled = True   # 안전망 발동 시 save 게이트도 강제 오픈
            except Exception:
                pass

    def _on_root_unmap(self, event) -> None:
        """(B) 최소화 직전 — 현재 정상 geometry 를 기억."""
        if event.widget is not self.root:
            return
        if not getattr(self, "_geo_settled", False):
            return   # startup withdraw 단계 무시
        # Unmap 시점엔 이미 geometry 가 garbage 일 수 있으므로 직전 Configure 에서
        # 잡아둔 _last_normal_geo 를 그대로 사용 (여기선 갱신하지 않음).

    def _on_root_map(self, event) -> None:
        """(B) 최소화→복원 시 마지막 정상 geometry 를 재적용."""
        if event.widget is not self.root:
            return
        if not getattr(self, "_geo_settled", False):
            return
        target = getattr(self, "_last_normal_geo", None)
        if not target:
            return
        # 약간 늦춰 적용 — WM 의 복원 처리가 끝난 뒤 덮어써야 안정적
        self.root.after(60, lambda: self._reapply_geo(target))

    def _reapply_geo(self, target: str) -> None:
        """현재 크기가 기준과 다르면 기준 geometry 로 되돌림 (사용자 크기 보존)."""
        try:
            if self.root.state() == "iconic":
                return
            cur = self.root.geometry()
        except Exception:
            return
        if cur == target:
            return
        parsed = self._parse_geo(target)
        if not parsed:
            return
        w, h, _, _ = parsed
        if not self._geo_size_sane(w, h):
            return
        try:
            self.root.geometry(target)
        except Exception:
            pass

    def _on_configure_debounced(self, event) -> None:
        """창 크기/이동 시 500ms 디바운스 후 geometry 저장."""
        # 자식 위젯의 Configure 이벤트는 무시 (root 창만)
        if event.widget is not self.root:
            return
        # (B) 정상 상태의 유효 geometry 를 즉시(동기) 기억 — 최소화 복원 기준값.
        #     iconic/garbage 는 제외해야 하므로 검증 통과 시에만 갱신.
        if getattr(self, "_geo_settled", False):
            try:
                if self.root.state() == "normal":
                    geo = self.root.geometry()
                    parsed = self._parse_geo(geo)
                    if parsed:
                        w, h, x, y = parsed
                        if self._geo_size_sane(w, h) and x > -50 and y > -50:
                            self._last_normal_geo = geo
            except Exception:
                pass
        if self._geo_save_after is not None:
            try:
                self.root.after_cancel(self._geo_save_after)
            except Exception:
                pass
        self._geo_save_after = self.root.after(500, self._save_window_geometry)

    def _save_window_geometry(self, *, force: bool = False) -> None:
        """현재 창 위치/크기를 gui_state.json에 저장.

        force=False (기본): settle 기간 중에는 skip + 최소화(iconic) 상태일 때도 skip.
        force=True: 사용자 종료 시점 등 명시적 호출 — 가드 무시.
        """
        import json
        if not force:
            # 스타트업 settle 중에는 자동 발생한 save 무시 (pywinstyles 부작용 흡수)
            if not getattr(self, "_geo_settled", False):
                return
            # 최소화/iconified 상태에서는 geometry 가 garbage(예: -32000) 일 수 있음
            try:
                if self.root.state() == "iconic":
                    return
            except Exception:
                pass
        try:
            geo = self.root.geometry()   # "920x680+100+200" 형태
            # 추가 sanity (D): 좌표가 화면 밖(-50 미만)이거나 크기가 비정상
            # (minsize 미만 — 최소화 전이 중 1x1 등)이면 저장하지 않음.
            parsed = self._parse_geo(geo)
            if parsed and not force:
                w, h, x, y = parsed
                if x < -50 or y < -50:
                    return
                if w < self._MIN_W or h < self._MIN_H:
                    return
            state = {
                "geometry": geo,
                "status_expanded": self._status_expanded,
            }
            self._GUI_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(self._GUI_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f)
        except Exception:
            pass   # GUI 종료 시 저장 실패해도 무시

    def _on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askokcancel(
                "작업 진행 중",
                "작업이 진행 중입니다.\n정말 종료하시겠습니까?",
            ):
                return
        # sleep 방지 자식 프로세스 정리 (atexit도 있지만 안전 차원 한 번 더)
        try:
            self._sleep_inhibitor.stop()
        except Exception:
            pass
        # 종료 시 저장 — settle 가드는 그대로 존중 (settle 전 닫으면 skip,
        # 마지막 저장된 위치 유지). settle 후엔 Configure 디바운스가 이미
        # 최신 위치를 저장했을 가능성이 높지만 보험 차원에서 한 번 더.
        self._save_window_geometry()
        self.root.destroy()


# ──────────────────────────────────────────────────────────
# 동기화 폴더 관리 대화상자
# ──────────────────────────────────────────────────────────

class FolderManagerDialog(tk.Toplevel):
    """동기화 폴더 쌍을 마우스로 추가/수정/제거하는 대화상자.

    config.yaml의 sync_pairs 섹션을 안전하게 편집.
    YAML 문법을 몰라도 됨.
    """

    def __init__(self, parent: tk.Tk, on_save=None):
        super().__init__(parent)
        self.title("동기화 폴더 관리")
        self.geometry("760x440")
        self.minsize(620, 340)
        self.transient(parent)
        self.grab_set()

        self.on_save_callback = on_save
        self.pairs: list[dict] = []
        self._load_pairs()
        self._build_ui()
        self._refresh_list()

        # 중앙 정렬
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _load_pairs(self) -> None:
        try:
            cfg = load_config()
            for p in cfg.sync_pairs:
                self.pairs.append({
                    "local": str(p.local_path),
                    "remote": p.remote_path,
                })
        except FileNotFoundError:
            pass
        except Exception as e:
            messagebox.showwarning(
                "설정 로드 경고",
                f"기존 설정을 읽는 중 문제 발생:\n{e}\n\n빈 목록에서 시작합니다.",
                parent=self,
            )

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        # 설명
        ttk.Label(
            main,
            text="동기화할 폴더 쌍을 관리합니다. 여러 개를 추가할 수 있습니다.",
            font=("Segoe UI", 9) if sys.platform == "win32" else ("Helvetica", 11),
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            main,
            text="좌측: 로컬 PC의 폴더   /   우측: 구글드라이브 상의 폴더 경로",
            foreground="gray",
        ).pack(anchor="w", pady=(0, 8))

        # Treeview
        tree_frame = ttk.Frame(main)
        tree_frame.pack(fill="both", expand=True)

        columns = ("local", "remote")
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
            height=8,
        )
        self.tree.heading("local", text="로컬 폴더")
        self.tree.heading("remote", text="구글드라이브 경로")
        self.tree.column("local", width=420, anchor="w")
        self.tree.column("remote", width=260, anchor="w")

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        # 더블클릭으로 수정
        self.tree.bind("<Double-1>", lambda e: self._on_edit())

        # 조작 버튼 행
        op_frame = ttk.Frame(main)
        op_frame.pack(fill="x", pady=(10, 0))

        ttk.Button(op_frame, text="+ 추가", command=self._on_add, width=10).pack(
            side="left", padx=(0, 4),
        )
        ttk.Button(op_frame, text="✎ 수정", command=self._on_edit, width=10).pack(
            side="left", padx=4,
        )
        ttk.Button(op_frame, text="− 제거", command=self._on_remove, width=10).pack(
            side="left", padx=4,
        )
        ttk.Button(op_frame, text="↑ 위로", command=lambda: self._move(-1), width=10).pack(
            side="left", padx=4,
        )
        ttk.Button(op_frame, text="↓ 아래로", command=lambda: self._move(1), width=10).pack(
            side="left", padx=4,
        )

        # 저장/취소 버튼 행
        save_frame = ttk.Frame(main)
        save_frame.pack(fill="x", pady=(14, 0))

        self.count_var = tk.StringVar(value="")
        ttk.Label(save_frame, textvariable=self.count_var, foreground="gray").pack(side="left")

        ttk.Button(save_frame, text="취소", command=self.destroy, width=10).pack(
            side="right", padx=(4, 0),
        )
        ttk.Button(
            save_frame, text="저장", command=self._on_save, width=10,
        ).pack(side="right", padx=4)

    def _refresh_list(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, p in enumerate(self.pairs):
            self.tree.insert("", "end", iid=str(i), values=(p["local"], p["remote"]))
        self.count_var.set(f"{len(self.pairs)}개 폴더 쌍")

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _on_add(self) -> None:
        dlg = PairEditDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self.pairs.append(dlg.result)
            self._refresh_list()
            self.tree.selection_set(str(len(self.pairs) - 1))
            self.tree.see(str(len(self.pairs) - 1))

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("알림", "수정할 항목을 선택하세요.", parent=self)
            return
        dlg = PairEditDialog(self, initial=self.pairs[idx])
        self.wait_window(dlg)
        if dlg.result:
            self.pairs[idx] = dlg.result
            self._refresh_list()
            self.tree.selection_set(str(idx))

    def _on_remove(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        p = self.pairs[idx]
        if not messagebox.askyesno(
            "제거 확인",
            f"다음 동기화 쌍을 목록에서 제거하시겠습니까?\n\n"
            f"로컬:    {p['local']}\n"
            f"리모트:  {p['remote']}\n\n"
            f"(실제 폴더나 파일은 삭제되지 않습니다)",
            parent=self,
        ):
            return
        del self.pairs[idx]
        self._refresh_list()

    def _move(self, delta: int) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        new_idx = idx + delta
        if not (0 <= new_idx < len(self.pairs)):
            return
        self.pairs[idx], self.pairs[new_idx] = self.pairs[new_idx], self.pairs[idx]
        self._refresh_list()
        self.tree.selection_set(str(new_idx))

    def _on_save(self) -> None:
        # 중복 로컬 경로 검사
        seen: set[str] = set()
        for p in self.pairs:
            key = p["local"].lower() if sys.platform == "win32" else p["local"]
            if key in seen:
                messagebox.showerror(
                    "중복 오류",
                    f"같은 로컬 폴더가 두 번 지정되었습니다:\n{p['local']}",
                    parent=self,
                )
                return
            seen.add(key)

        if not self.pairs:
            if not messagebox.askyesno(
                "확인",
                "동기화 폴더가 하나도 없습니다.\n정말 저장하시겠습니까? (동기화 불가)",
                parent=self,
            ):
                return

        try:
            self._save_to_config()
        except Exception as e:
            messagebox.showerror("저장 실패", f"설정 파일 저장 실패:\n{e}", parent=self)
            return

        if self.on_save_callback:
            try:
                self.on_save_callback()
            except Exception:
                pass

        messagebox.showinfo(
            "저장 완료",
            f"{len(self.pairs)}개 폴더 쌍이 저장되었습니다.",
            parent=self,
        )
        self.destroy()

    def _save_to_config(self) -> None:
        """기존 config.yaml의 다른 섹션(network, bandwidth 등)은 유지하고
        sync_pairs만 교체.

        device_overrides.<hostname>.sync_pairs가 있으면 자동으로 제거한다.
        (로더가 device_overrides로 최상위를 덮어쓰기 때문에 두 곳이 공존하면
         GUI 저장이 무시되는 버그 발생. 호스트 항목의 다른 필드는 유지.)
        """
        import yaml
        import socket
        path = DEFAULT_CONFIG_PATH

        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        else:
            raw = default_config_template()

        raw["sync_pairs"] = [
            {"local_path": p["local"], "remote_path": p["remote"]}
            for p in self.pairs
        ]

        # device_overrides.<hostname>.sync_pairs 자동 정리
        hostname = socket.gethostname()
        overrides = raw.get("device_overrides")
        if isinstance(overrides, dict):
            host_override = overrides.get(hostname)
            if isinstance(host_override, dict) and "sync_pairs" in host_override:
                del host_override["sync_pairs"]
                # 호스트 항목이 비면 삭제
                if not host_override:
                    del overrides[hostname]
                # device_overrides 섹션 자체가 비면 삭제
                if not overrides:
                    del raw["device_overrides"]

        save_config(raw, path)


# ──────────────────────────────────────────────────────────
# 폴더 쌍 1개 추가/수정 대화상자
# ──────────────────────────────────────────────────────────

class PairEditDialog(tk.Toplevel):
    """폴더 쌍 한 개를 입력/편집. 결과는 self.result (dict or None)."""

    def __init__(self, parent: tk.Toplevel, initial: Optional[dict] = None):
        super().__init__(parent)
        self.title("동기화 폴더 쌍")
        self.geometry("620x260")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[dict] = None
        initial = initial or {"local": "", "remote": ""}

        self._build_ui(initial)

        # 중앙 정렬
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        # Enter = 확인, Esc = 취소
        self.bind("<Return>", lambda e: self._on_ok())
        self.bind("<Escape>", lambda e: self.destroy())

    def _build_ui(self, initial: dict) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)

        # 로컬 폴더
        ttk.Label(main, text="로컬 폴더 (이 PC의 경로):").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4),
        )

        self.local_var = tk.StringVar(value=initial["local"])
        local_entry = ttk.Entry(main, textvariable=self.local_var)
        local_entry.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 6))

        ttk.Button(main, text="찾아보기...", command=self._browse_local, width=12).grid(
            row=1, column=2,
        )

        ttk.Label(
            main,
            text="예: C:/Users/user/Documents/업무   또는   ~/Projects/2026",
            foreground="gray",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 14))

        # 리모트 경로
        ttk.Label(main, text="구글드라이브 폴더 경로:").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(0, 4),
        )

        self.remote_var = tk.StringVar(value=initial["remote"])
        ttk.Entry(main, textvariable=self.remote_var).grid(
            row=4, column=0, columnspan=3, sticky="ew",
        )

        ttk.Label(
            main,
            text="예: 업무/2026   (슬래시 / 로 하위 폴더 지정. 없으면 자동 생성됨)",
            foreground="gray",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 버튼
        btn = ttk.Frame(main)
        btn.grid(row=6, column=0, columnspan=3, sticky="e", pady=(18, 0))
        ttk.Button(btn, text="취소", command=self.destroy, width=10).pack(
            side="right", padx=(4, 0),
        )
        ttk.Button(btn, text="확인", command=self._on_ok, width=10).pack(
            side="right", padx=4,
        )

        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=1)

        local_entry.focus_set()

    def _browse_local(self) -> None:
        current = self.local_var.get().strip()
        # 기존 경로가 유효하면 거기서 시작, 아니면 홈
        if current:
            p = Path(current).expanduser()
            initialdir = str(p) if p.exists() else str(Path.home())
        else:
            initialdir = str(Path.home())

        selected = filedialog.askdirectory(
            parent=self,
            title="동기화할 로컬 폴더 선택",
            initialdir=initialdir,
            mustexist=False,
        )
        if selected:
            self.local_var.set(selected)

    def _on_ok(self) -> None:
        local = self.local_var.get().strip()
        remote = self.remote_var.get().strip().strip("/")

        if not local:
            messagebox.showwarning("입력 필요", "로컬 폴더를 입력하세요.", parent=self)
            return
        if not remote:
            messagebox.showwarning(
                "입력 필요", "구글드라이브 폴더 경로를 입력하세요.", parent=self,
            )
            return

        self.result = {"local": local, "remote": remote}
        self.destroy()


# ──────────────────────────────────────────────────────────
# 개별 동기화 선택 대화상자
# ──────────────────────────────────────────────────────────

class SingleSyncDialog(tk.Toplevel):
    """동기화 쌍을 선택하는 대화상자 (다중 선택 지원).

    mode:
        "sync"    → 선택한 폴더 즉시 동기화
        "preview" → 선택한 폴더 미리보기 (dry-run)
    """

    def __init__(self, parent: tk.Tk, sync_pairs, mode: str = "sync"):
        super().__init__(parent)
        self.mode = mode
        if mode == "preview":
            self.title("개별 미리보기 — 폴더 선택")
            self.action_label = "미리보기"
        else:
            self.title("개별 동기화 — 폴더 선택")
            self.action_label = "동기화"

        self.geometry("620x420")
        self.resizable(True, True)
        self.transient(parent)
        self.grab_set()

        # 결과: 선택된 인덱스 리스트 (빈 리스트면 취소)
        self.selected_indices: list[int] = []

        self._build_ui(sync_pairs)

        # 중앙 정렬
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.bind("<Escape>", lambda e: self.destroy())

    def _build_ui(self, sync_pairs) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        ttk.Label(
            main,
            text=f"{self.action_label}할 폴더를 선택하세요 (여러 개 선택 가능):",
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            main,
            text="Ctrl+클릭: 개별 추가, Shift+클릭: 범위 선택",
            foreground="gray",
        ).pack(anchor="w", pady=(0, 8))

        # Listbox (다중 선택)
        list_frame = ttk.Frame(main)
        list_frame.pack(fill="both", expand=True)

        self.listbox = tk.Listbox(
            list_frame,
            selectmode="extended",   # 다중 선택
            font=("Segoe UI", 9) if sys.platform == "win32" else ("Helvetica", 11),
            exportselection=False,
            background=CLAUDE_COLORS["bg_elevated"],
            foreground=CLAUDE_COLORS["text_primary"],
            selectbackground=CLAUDE_COLORS["accent_light"],
            selectforeground=CLAUDE_COLORS["accent"],
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground=CLAUDE_COLORS["border"],
            highlightcolor=CLAUDE_COLORS["accent"],
        )
        ysb = ttk.Scrollbar(list_frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)

        for i, pair in enumerate(sync_pairs):
            local_name = pair.local_path.name or str(pair.local_path)
            self.listbox.insert("end", f"  {local_name}    ↔    {pair.remote_path}")
        if sync_pairs:
            self.listbox.selection_set(0)

        # 선택 버튼 + 액션 버튼
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(10, 0))

        # 좌측: 전체 선택 / 선택 해제
        ttk.Button(
            btn_frame, text="전체 선택",
            command=lambda: self.listbox.select_set(0, "end"),
            width=10,
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            btn_frame, text="선택 해제",
            command=lambda: self.listbox.selection_clear(0, "end"),
            width=10,
        ).pack(side="left", padx=4)

        # 선택 개수 표시
        self._count_var = tk.StringVar(value="")
        ttk.Label(btn_frame, textvariable=self._count_var, foreground="gray").pack(
            side="left", padx=(10, 0),
        )
        self.listbox.bind("<<ListboxSelect>>", self._update_count)
        self._update_count(None)

        # 우측: 취소 / 실행
        ttk.Button(btn_frame, text="취소", command=self.destroy, width=10).pack(
            side="right", padx=(4, 0),
        )
        ttk.Button(
            btn_frame, text=f"✓ {self.action_label}",
            command=self._on_confirm, width=12,
        ).pack(side="right", padx=4)

    def _update_count(self, _event) -> None:
        n = len(self.listbox.curselection())
        total = self.listbox.size()
        self._count_var.set(f"{n}/{total}개 선택됨")

    def _on_confirm(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            messagebox.showinfo("알림", f"{self.action_label}할 폴더를 선택하세요.", parent=self)
            return
        self.selected_indices = list(sel)
        self.destroy()


# ──────────────────────────────────────────────────────────
# 동기화 옵션 선택 대화상자 (방향 + PC 종료)
# ──────────────────────────────────────────────────────────

class SyncOptionsDialog(tk.Toplevel):
    """동기화 시작 전 방향과 PC 종료 여부를 선택하는 대화상자.

    - 방향: 양방향(기본) / 업로드만(로컬→Drive) / 다운로드만(Drive→로컬)
    - 완료 후 PC 종료 체크박스
    - 결과: self.confirmed (True=시작), self.force_mode, self.shutdown_after
    """

    def __init__(self, parent, title: str = "동기화 옵션"):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.confirmed: bool = False
        self.force_mode = None  # None | "upload" | "download"
        self.shutdown_after: bool = False

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)

        ttk.Label(
            main,
            text="동기화 방향을 선택하세요",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        self._direction_var = tk.StringVar(value="bidirectional")

        options = [
            (
                "bidirectional",
                "양방향 (기본)",
                "최신 수정 파일 기준으로 로컬 ↔ Drive 자동 동기화",
            ),
            (
                "upload",
                "업로드만 (로컬 → Drive 미러)",
                "⚠ Drive를 로컬과 100% 동일하게 만듭니다. "
                "Drive에만 있는 파일은 휴지통으로 이동됩니다.",
            ),
            (
                "download",
                "다운로드만 (Drive → 로컬 미러)",
                "⚠ 로컬을 Drive와 100% 동일하게 만듭니다. "
                "로컬에만 있는 파일은 휴지통으로 이동됩니다.",
            ),
        ]

        for value, label, desc in options:
            frame = ttk.Frame(main)
            frame.pack(fill="x", pady=2)
            ttk.Radiobutton(
                frame, text=label, value=value, variable=self._direction_var,
            ).pack(anchor="w")
            ttk.Label(
                frame, text=f"    {desc}", foreground="gray",
            ).pack(anchor="w")

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=12)

        self._shutdown_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            main,
            text="완료 후 PC 종료 (60초 카운트다운, 취소 가능)",
            variable=self._shutdown_var,
        ).pack(anchor="w")

        ttk.Separator(main, orient="horizontal").pack(fill="x", pady=12)

        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x")

        ttk.Button(
            btn_frame, text="취소", width=10,
            command=self._on_cancel,
        ).pack(side="right", padx=(6, 0))
        ttk.Button(
            btn_frame, text="시작", width=10,
            style="Accent.TButton",
            command=self._on_start,
        ).pack(side="right")

        # 엔터=시작, ESC=취소
        self.bind("<Return>", lambda e: self._on_start())
        self.bind("<Escape>", lambda e: self._on_cancel())

        # 창 중앙 정렬 & 크기 자동
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        px = parent.winfo_rootx() + (parent.winfo_width() - w) // 2
        py = parent.winfo_rooty() + (parent.winfo_height() - h) // 2
        self.geometry(f"+{max(0, px)}+{max(0, py)}")

    def _on_start(self) -> None:
        dir_val = self._direction_var.get()
        if dir_val == "upload":
            self.force_mode = "upload"
        elif dir_val == "download":
            self.force_mode = "download"
        else:
            self.force_mode = None
        self.shutdown_after = bool(self._shutdown_var.get())
        self.confirmed = True
        self.destroy()

    def _on_cancel(self) -> None:
        self.confirmed = False
        self.destroy()


# ──────────────────────────────────────────────────────────
# 미리보기 결과 대화상자 (시간 예상 + 동기화 진행 확인)
# ──────────────────────────────────────────────────────────

class PreviewResultDialog(tk.Toplevel):
    """미리보기 완료 후 결과를 요약하고 실제 동기화 진행 여부를 묻는 대화상자.

    포함 정보:
    - 업로드/다운로드/삭제/충돌 개수 및 바이트 집계
    - 현재 활성 대역폭 규칙 + 예상 소요 시간
    - "완료 후 PC 종료" 체크박스
    - 결과: self.proceed_with_sync (True면 동기화 진행), self.shutdown_after
    """

    def __init__(self, parent, all_summaries: list, cfg):
        super().__init__(parent)
        # 최소화 상태에서 자동 표시될 때 보이지 않는 모달 grab 데드락 방지
        _ensure_viewable(parent)
        self.title("미리보기 결과")
        self.geometry("560x480")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.proceed_with_sync: bool = False
        self.shutdown_after: bool = False

        self._build_ui(all_summaries, cfg)

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.bind("<Escape>", lambda e: self.destroy())

    def _build_ui(self, all_summaries, cfg) -> None:
        from gdrive_sync.sync_engine import ActionType
        from gdrive_sync.utils import human_size, format_duration

        main = ttk.Frame(self, padding=16)
        main.pack(fill="both", expand=True)

        # ── 집계
        UPLOAD_TYPES = {ActionType.UPLOAD_NEW, ActionType.UPLOAD_UPDATE, ActionType.CONFLICT_UPLOAD}
        DOWNLOAD_TYPES = {ActionType.DOWNLOAD_NEW, ActionType.DOWNLOAD_UPDATE, ActionType.CONFLICT_DOWNLOAD}
        DELETE_TYPES = {ActionType.DELETE_LOCAL, ActionType.DELETE_REMOTE}

        up_count = up_bytes = 0
        dn_count = dn_bytes = 0
        del_count = 0
        conflict_count = 0
        skip_count = 0

        for pair, summary in all_summaries:
            for a in summary.actions:
                if a.type in UPLOAD_TYPES:
                    up_count += 1
                    up_bytes += (a.local.size if a.local else 0)
                elif a.type in DOWNLOAD_TYPES:
                    dn_count += 1
                    dn_bytes += (a.remote.size if a.remote else 0)
                elif a.type in DELETE_TYPES:
                    del_count += 1
                elif a.type == ActionType.CONFLICT_KEEP_BOTH:
                    conflict_count += 1
                elif "skip" in a.type.value:
                    skip_count += 1

        # ── 대역폭 → 시간 예상
        limiter = make_limiter(cfg.bandwidth)
        if limiter:
            st = limiter.get_status()
            up_mbps = st["upload_mbps"] if not st["upload_unlimited"] else 10.0
            dn_mbps = st["download_mbps"] if not st["download_unlimited"] else 10.0
            rule_name = st["active_rule"]
            bw_desc = (
                f"↑{'무제한' if st['upload_unlimited'] else f'{up_mbps} MB/s'}   "
                f"↓{'무제한' if st['download_unlimited'] else f'{dn_mbps} MB/s'}"
            )
        else:
            up_mbps = dn_mbps = 10.0     # 무제한 추정치
            rule_name = "제한 없음"
            bw_desc = "무제한 (약 10 MB/s 추정)"

        # 초 단위 예상 (업로드 + 다운로드 시간, 병렬이지만 대역폭은 공유)
        up_seconds = up_bytes / (up_mbps * 1024 * 1024) if up_mbps > 0 else 0
        dn_seconds = dn_bytes / (dn_mbps * 1024 * 1024) if dn_mbps > 0 else 0
        total_seconds = up_seconds + dn_seconds + 5   # API 오버헤드 5초 버퍼

        # ── 제목
        ttk.Label(
            main, text="미리보기 결과 (실제 전송 없음)",
            font=("Segoe UI", 11, "bold") if sys.platform == "win32" else ("Helvetica", 13, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        # ── 요약 박스
        summary_frame = ttk.LabelFrame(main, text="  전송 요약  ", padding=12)
        summary_frame.pack(fill="x", pady=(0, 10))

        summary_text = (
            f"↑ 업로드:      {up_count:>5}개    ({human_size(up_bytes)})\n"
            f"↓ 다운로드:    {dn_count:>5}개    ({human_size(dn_bytes)})\n"
            f"✕ 삭제:        {del_count:>5}개\n"
            f"⚡ 충돌:        {conflict_count:>5}개\n"
            f"⏭ 변경 없음:   {skip_count:>5}개"
        )
        ttk.Label(
            summary_frame, text=summary_text,
            font=("Consolas", 10) if sys.platform == "win32" else ("Menlo", 11),
            justify="left",
        ).pack(anchor="w")

        # ── 시간 예상 박스
        time_frame = ttk.LabelFrame(main, text="  예상 소요 시간  ", padding=12)
        time_frame.pack(fill="x", pady=(0, 10))

        if up_count == 0 and dn_count == 0:
            duration_text = "변경 없음 (전송 불필요)"
            color = "gray"
        else:
            duration_text = f"~ {format_duration(total_seconds)}"
            color = "black"

        ttk.Label(
            time_frame, text=duration_text,
            font=("Segoe UI", 12, "bold") if sys.platform == "win32" else ("Helvetica", 14, "bold"),
            foreground=color,
        ).pack(anchor="w")

        ttk.Label(
            time_frame,
            text=f"현재 대역폭 규칙: [{rule_name}]  {bw_desc}",
            foreground="gray",
        ).pack(anchor="w", pady=(4, 0))

        # ── PC 종료 옵션
        option_frame = ttk.Frame(main)
        option_frame.pack(fill="x", pady=(0, 10))

        self.shutdown_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            option_frame,
            text="🖥  동기화 완료 후 PC 종료",
            variable=self.shutdown_var,
        ).pack(anchor="w")

        # ── 버튼
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(10, 0))

        ttk.Button(btn_frame, text="닫기", command=self.destroy, width=10).pack(
            side="right", padx=(4, 0),
        )

        sync_btn = ttk.Button(
            btn_frame, text="✓ 동기화 진행",
            command=self._on_proceed, width=14,
        )
        sync_btn.pack(side="right", padx=4)

        # 변경 없으면 동기화 버튼 비활성화
        if up_count == 0 and dn_count == 0 and del_count == 0 and conflict_count == 0:
            sync_btn.config(state="disabled")

    def _on_proceed(self) -> None:
        self.proceed_with_sync = True
        self.shutdown_after = self.shutdown_var.get()
        self.destroy()


# ──────────────────────────────────────────────────────────
# 전체 로그 뷰어 대화상자
# ──────────────────────────────────────────────────────────

class LogViewerDialog(tk.Toplevel):
    """전체 로그 파일 조회 대화상자 (RotatingFileHandler 백업본 포함)."""

    _LOG_COLORS = {
        "ERROR":    "#C9241C",
        "CRITICAL": "#C9241C",
        "WARNING":  "#B25000",
        "INFO":     "#3C3C43",
        "DEBUG":    "#8E8E93",
    }

    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        self.title("전체 로그 보기")
        self.geometry("900x600")
        self.minsize(700, 400)
        self.transient(parent)
        self.grab_set()

        self._all_lines: list[tuple[str, str]] = []  # (level, line_text)
        self._filter_var = tk.StringVar(value="ALL")

        self._build_ui()
        self._load_logs()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        # ── 툴바
        toolbar = ttk.Frame(main)
        toolbar.pack(fill="x", pady=(0, 6))

        ttk.Label(toolbar, text="필터:").pack(side="left")
        for label, val in [("전체", "ALL"), ("오류만", "ERROR"), ("경고 이상", "WARNING")]:
            ttk.Radiobutton(
                toolbar, text=label, variable=self._filter_var, value=val,
                command=self._apply_filter,
            ).pack(side="left", padx=4)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", padx=8, fill="y", pady=2)

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Label(toolbar, text="검색:").pack(side="left")
        search_entry = ttk.Entry(toolbar, textvariable=self._search_var, width=22)
        search_entry.pack(side="left", padx=(2, 8))

        ttk.Button(toolbar, text="저장 (다운로드)...", command=self._save_log).pack(side="right")
        ttk.Button(toolbar, text="새로고침", command=self._load_logs).pack(side="right", padx=(0, 4))

        # ── 로그 텍스트 영역
        log_font = ("Consolas", 9) if sys.platform == "win32" else ("Menlo", 11)
        text_frame = ttk.Frame(main)
        text_frame.pack(fill="both", expand=True)

        self._text = tk.Text(
            text_frame, wrap="none", state="disabled",
            font=log_font,
            background=CLAUDE_COLORS["log_bg"],
            foreground=CLAUDE_COLORS["log_fg"],
        )
        ysb = ttk.Scrollbar(text_frame, orient="vertical", command=self._text.yview)
        xsb = ttk.Scrollbar(text_frame, orient="horizontal", command=self._text.xview)
        self._text.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)

        xsb.pack(side="bottom", fill="x")
        ysb.pack(side="right", fill="y")
        self._text.pack(side="left", fill="both", expand=True)

        for level, color in self._LOG_COLORS.items():
            self._text.tag_configure(level, foreground=color)
        self._text.tag_configure("CRITICAL", foreground=self._LOG_COLORS["CRITICAL"],
                                  font=(log_font[0], log_font[1], "bold"))

        # ── 하단 상태줄
        bottom = ttk.Frame(main)
        bottom.pack(fill="x", pady=(6, 0))
        self._status_var = tk.StringVar(value="로그 불러오는 중…")
        ttk.Label(bottom, textvariable=self._status_var, foreground="gray").pack(side="left")
        ttk.Button(bottom, text="닫기", command=self.destroy, width=10).pack(side="right")

    def _collect_log_files(self) -> list[Path]:
        """로그 파일 목록 (최신 순): gdrive_sync.log, .log.1, .log.2 …"""
        try:
            from gdrive_sync.config import DEFAULT_CONFIG_DIR, load_config
            cfg = load_config()
            base = DEFAULT_CONFIG_DIR / cfg.log_file
        except Exception:
            from gdrive_sync.config import DEFAULT_CONFIG_DIR
            base = DEFAULT_CONFIG_DIR / "gdrive_sync.log"

        files: list[Path] = []
        if base.exists():
            files.append(base)
        for i in range(1, 10):
            rotated = base.parent / (base.name + f".{i}")
            if rotated.exists():
                files.append(rotated)
        return files

    def _parse_level(self, line: str) -> str:
        for level in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
            if f"[{level}]" in line:
                return level
        return "INFO"

    def _load_logs(self) -> None:
        """모든 로그 파일을 읽어 self._all_lines에 적재 후 표시."""
        self._all_lines = []
        files = self._collect_log_files()
        total_bytes = 0

        for log_path in files:
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
                total_bytes += log_path.stat().st_size
                for line in text.splitlines():
                    if line.strip():
                        self._all_lines.append((self._parse_level(line), line))
            except Exception:
                pass

        file_count = len(files)
        total_kb = total_bytes / 1024
        label = f"{file_count}개 파일 · {len(self._all_lines):,}줄 · {total_kb:.0f} KB"
        self._status_var.set(label)
        self._apply_filter()

    def _apply_filter(self) -> None:
        mode = self._filter_var.get()
        keyword = self._search_var.get().lower()

        _show_levels = {
            "ALL":     {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"},
            "WARNING": {"WARNING", "ERROR", "CRITICAL"},
            "ERROR":   {"ERROR", "CRITICAL"},
        }.get(mode, {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})

        self._text.config(state="normal")
        self._text.delete("1.0", "end")

        shown = 0
        for level, line in self._all_lines:
            if level not in _show_levels:
                continue
            if keyword and keyword not in line.lower():
                continue
            self._text.insert("end", line + "\n", level)
            shown += 1

        self._text.config(state="disabled")
        self._text.see("end")

        mode_label = {"ALL": "전체", "WARNING": "경고 이상", "ERROR": "오류만"}.get(mode, mode)
        kw_label = f" · 검색: '{self._search_var.get()}'" if keyword else ""
        self._status_var.set(
            f"{len(self._collect_log_files())}개 파일 · 표시 {shown:,}줄 / 전체 {len(self._all_lines):,}줄"
            f" [{mode_label}{kw_label}]"
        )

    def _save_log(self) -> None:
        """전체 로그를 단일 .txt 파일로 저장."""
        import datetime
        default_name = f"gdrive_sync_log_{datetime.date.today().isoformat()}.txt"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="로그 파일 저장",
            initialfile=default_name,
            defaultextension=".txt",
            filetypes=[("텍스트 파일", "*.txt"), ("모든 파일", "*.*")],
        )
        if not path:
            return
        try:
            lines = [line for _, line in self._all_lines]
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
            messagebox.showinfo("저장 완료", f"로그가 저장됐습니다:\n{path}", parent=self)
        except Exception as e:
            messagebox.showerror("저장 실패", str(e), parent=self)


# ──────────────────────────────────────────────────────────
# 동기화 히스토리 대화상자
# ──────────────────────────────────────────────────────────

class HistoryDialog(tk.Toplevel):
    """동기화 히스토리 조회 대화상자."""

    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        # 동기화 완료 후 자동 표시 — 창이 최소화돼 있으면 먼저 복원
        # (보이지 않는 모달 grab 으로 메인 창이 안 나오는 데드락 방지)
        _ensure_viewable(parent)
        self.title("동기화 히스토리")
        self.geometry("780x520")
        self.minsize(640, 400)
        self.transient(parent)
        self.grab_set()

        self._build_ui()

        # 중앙 정렬
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        # 통계 요약
        history = load_history()
        stats = get_stats(history)

        stats_frame = ttk.LabelFrame(main, text="  통계 요약  ", padding=8)
        stats_frame.pack(fill="x", pady=(0, 8))

        def _fmt_bytes(b: int) -> str:
            if b < 1024:
                return f"{b} B"
            if b < 1024 * 1024:
                return f"{b / 1024:.1f} KB"
            if b < 1024 * 1024 * 1024:
                return f"{b / (1024 * 1024):.1f} MB"
            return f"{b / (1024 * 1024 * 1024):.2f} GB"

        stats_text = (
            f"총 동기화: {stats['total_syncs']}회    "
            f"성공률: {stats['success_rate']:.0f}%    "
            f"총 업로드: {stats['total_uploaded']}개 ({_fmt_bytes(stats['total_uploaded_bytes'])})    "
            f"총 다운로드: {stats['total_downloaded']}개 ({_fmt_bytes(stats['total_downloaded_bytes'])})    "
            f"총 오류: {stats['total_errors']}개"
        )
        ttk.Label(stats_frame, text=stats_text, wraplength=700).pack(anchor="w")

        # Treeview
        tree_frame = ttk.Frame(main)
        tree_frame.pack(fill="both", expand=True)

        columns = ("date", "uploaded", "downloaded", "errors", "elapsed")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=15)
        self.tree.heading("date", text="날짜")
        self.tree.heading("uploaded", text="업로드")
        self.tree.heading("downloaded", text="다운로드")
        self.tree.heading("errors", text="오류")
        self.tree.heading("elapsed", text="소요시간")

        self.tree.column("date", width=180, anchor="w")
        self.tree.column("uploaded", width=100, anchor="center")
        self.tree.column("downloaded", width=100, anchor="center")
        self.tree.column("errors", width=80, anchor="center")
        self.tree.column("elapsed", width=100, anchor="center")

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        # 최신 순으로 표시
        for rec in reversed(history):
            ts = format_timestamp_local(rec.timestamp)
            dr = " (미리보기)" if rec.dry_run else ""
            self.tree.insert("", "end", values=(
                f"{ts}{dr}",
                rec.uploaded,
                rec.downloaded,
                rec.errors,
                f"{rec.elapsed_sec:.1f}초",
            ))

        # 닫기
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x", pady=(10, 0))
        ttk.Label(btn_frame, text=f"총 {len(history)}건", foreground="gray").pack(side="left")
        ttk.Button(btn_frame, text="닫기", command=self.destroy, width=10).pack(side="right")


# ──────────────────────────────────────────────────────────
# 대역폭 시간대 편집 대화상자
# ──────────────────────────────────────────────────────────

class BandwidthEditorDialog(tk.Toplevel):
    """대역폭 시간대별 규칙을 편집하는 대화상자."""

    def __init__(self, parent: tk.Tk, on_save=None):
        super().__init__(parent)
        self.title("대역폭 시간대 편집")
        self.geometry("820x520")
        self.minsize(700, 420)
        self.transient(parent)
        self.grab_set()

        self.on_save_callback = on_save
        self.rules: list[dict] = []
        self.default_upload = 0.0
        self.default_download = 0.0
        self._load_rules()
        self._build_ui()
        self._refresh_list()

        # 중앙 정렬
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _load_rules(self) -> None:
        try:
            cfg = load_config()
            self.default_upload = cfg.bandwidth.upload_limit_mbps
            self.default_download = cfg.bandwidth.download_limit_mbps
            for s in cfg.bandwidth.schedule:
                self.rules.append({
                    "name": s.name,
                    "time_start": s.time_start,
                    "time_end": s.time_end,
                    "weekdays": list(s.weekdays),
                    "upload_limit_mbps": s.upload_limit_mbps,
                    "download_limit_mbps": s.download_limit_mbps,
                })
        except Exception:
            pass

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        # 기본 제한 설정
        default_frame = ttk.LabelFrame(main, text="  기본 대역폭 제한 (스케줄 밖)  ", padding=8)
        default_frame.pack(fill="x", pady=(0, 8))

        ttk.Label(default_frame, text="업로드 제한 (MB/s, 0=무제한):").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.default_up_var = tk.StringVar(value=str(self.default_upload))
        ttk.Entry(default_frame, textvariable=self.default_up_var, width=10).grid(row=0, column=1, padx=(0, 20))

        ttk.Label(default_frame, text="다운로드 제한 (MB/s, 0=무제한):").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.default_dn_var = tk.StringVar(value=str(self.default_download))
        ttk.Entry(default_frame, textvariable=self.default_dn_var, width=10).grid(row=0, column=3)

        # Treeview
        tree_frame = ttk.Frame(main)
        tree_frame.pack(fill="both", expand=True)

        columns = ("name", "start", "end", "weekdays", "upload", "download")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=10)
        self.tree.heading("name", text="이름")
        self.tree.heading("start", text="시작")
        self.tree.heading("end", text="종료")
        self.tree.heading("weekdays", text="요일")
        self.tree.heading("upload", text="업로드(MB/s)")
        self.tree.heading("download", text="다운로드(MB/s)")

        self.tree.column("name", width=120)
        self.tree.column("start", width=70, anchor="center")
        self.tree.column("end", width=70, anchor="center")
        self.tree.column("weekdays", width=160)
        self.tree.column("upload", width=100, anchor="center")
        self.tree.column("download", width=100, anchor="center")

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        self.tree.bind("<Double-1>", lambda e: self._on_edit())

        # 버튼
        op_frame = ttk.Frame(main)
        op_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(op_frame, text="+ 추가", command=self._on_add, width=10).pack(side="left", padx=(0, 4))
        ttk.Button(op_frame, text="✎ 수정", command=self._on_edit, width=10).pack(side="left", padx=4)
        ttk.Button(op_frame, text="− 제거", command=self._on_remove, width=10).pack(side="left", padx=4)

        save_frame = ttk.Frame(main)
        save_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(save_frame, text="취소", command=self.destroy, width=10).pack(side="right", padx=(4, 0))
        ttk.Button(save_frame, text="저장", command=self._on_save, width=10).pack(side="right", padx=4)

    def _refresh_list(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        _WD_KR = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}
        for i, r in enumerate(self.rules):
            wd = ", ".join(_WD_KR.get(d, d) for d in r["weekdays"]) if r["weekdays"] else "매일"
            up = "무제한" if r["upload_limit_mbps"] == 0 else f"{r['upload_limit_mbps']}"
            dn = "무제한" if r["download_limit_mbps"] == 0 else f"{r['download_limit_mbps']}"
            self.tree.insert("", "end", iid=str(i), values=(
                r["name"], r["time_start"], r["time_end"], wd, up, dn,
            ))

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _on_add(self) -> None:
        dlg = BandwidthRuleEditDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self.rules.append(dlg.result)
            self._refresh_list()

    def _on_edit(self) -> None:
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("알림", "수정할 규칙을 선택하세요.", parent=self)
            return
        dlg = BandwidthRuleEditDialog(self, initial=self.rules[idx])
        self.wait_window(dlg)
        if dlg.result:
            self.rules[idx] = dlg.result
            self._refresh_list()

    def _on_remove(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        del self.rules[idx]
        self._refresh_list()

    def _on_save(self) -> None:
        try:
            up = float(self.default_up_var.get())
            dn = float(self.default_dn_var.get())
        except ValueError:
            messagebox.showerror("입력 오류", "기본 제한 값은 숫자로 입력하세요.", parent=self)
            return

        try:
            import yaml
            path = DEFAULT_CONFIG_PATH
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            else:
                raw = default_config_template()

            if "bandwidth" not in raw:
                raw["bandwidth"] = {}
            raw["bandwidth"]["upload_limit_mbps"] = up
            raw["bandwidth"]["download_limit_mbps"] = dn
            raw["bandwidth"]["schedule"] = [
                {
                    "name": r["name"],
                    "time_start": r["time_start"],
                    "time_end": r["time_end"],
                    "weekdays": r["weekdays"],
                    "upload_limit_mbps": r["upload_limit_mbps"],
                    "download_limit_mbps": r["download_limit_mbps"],
                }
                for r in self.rules
            ]
            save_config(raw, path)
            messagebox.showinfo("저장 완료", "대역폭 설정이 저장되었습니다.", parent=self)
            if self.on_save_callback:
                try:
                    self.on_save_callback()
                except Exception:
                    pass
            self.destroy()
        except Exception as e:
            messagebox.showerror("저장 실패", f"설정 저장 실패:\n{e}", parent=self)


class BandwidthRuleEditDialog(tk.Toplevel):
    """대역폭 규칙 한 개 입력/수정."""

    _WEEKDAYS = [("mon", "월"), ("tue", "화"), ("wed", "수"), ("thu", "목"),
                 ("fri", "금"), ("sat", "토"), ("sun", "일")]

    def __init__(self, parent: tk.Toplevel, initial: Optional[dict] = None):
        super().__init__(parent)
        self.title("대역폭 규칙 편집")
        self.geometry("480x320")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[dict] = None
        initial = initial or {
            "name": "", "time_start": "09:00", "time_end": "18:00",
            "weekdays": [], "upload_limit_mbps": 0, "download_limit_mbps": 0,
        }
        self._build_ui(initial)

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.bind("<Escape>", lambda e: self.destroy())

    def _build_ui(self, initial: dict) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)

        # 이름
        ttk.Label(main, text="규칙 이름:").grid(row=0, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=initial["name"])
        ttk.Entry(main, textvariable=self.name_var, width=20).grid(row=0, column=1, columnspan=3, sticky="w", pady=4)

        # 시간
        ttk.Label(main, text="시작 (HH:MM):").grid(row=1, column=0, sticky="w", pady=4)
        self.start_var = tk.StringVar(value=initial["time_start"])
        ttk.Entry(main, textvariable=self.start_var, width=8).grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(main, text="종료 (HH:MM):").grid(row=1, column=2, sticky="w", pady=4, padx=(10, 0))
        self.end_var = tk.StringVar(value=initial["time_end"])
        ttk.Entry(main, textvariable=self.end_var, width=8).grid(row=1, column=3, sticky="w", pady=4)

        # 요일
        ttk.Label(main, text="요일 (미선택=매일):").grid(row=2, column=0, sticky="w", pady=4)
        wd_frame = ttk.Frame(main)
        wd_frame.grid(row=2, column=1, columnspan=3, sticky="w", pady=4)

        self.wd_vars: dict[str, tk.BooleanVar] = {}
        existing_wd = set(initial.get("weekdays", []))
        for key, label in self._WEEKDAYS:
            var = tk.BooleanVar(value=(key in existing_wd))
            self.wd_vars[key] = var
            ttk.Checkbutton(wd_frame, text=label, variable=var).pack(side="left", padx=2)

        # 제한
        ttk.Label(main, text="업로드 (MB/s, 0=무제한):").grid(row=3, column=0, sticky="w", pady=4)
        self.up_var = tk.StringVar(value=str(initial["upload_limit_mbps"]))
        ttk.Entry(main, textvariable=self.up_var, width=10).grid(row=3, column=1, sticky="w", pady=4)

        ttk.Label(main, text="다운로드 (MB/s, 0=무제한):").grid(row=4, column=0, sticky="w", pady=4)
        self.dn_var = tk.StringVar(value=str(initial["download_limit_mbps"]))
        ttk.Entry(main, textvariable=self.dn_var, width=10).grid(row=4, column=1, sticky="w", pady=4)

        # 버튼
        btn = ttk.Frame(main)
        btn.grid(row=5, column=0, columnspan=4, sticky="e", pady=(14, 0))
        ttk.Button(btn, text="취소", command=self.destroy, width=10).pack(side="right", padx=(4, 0))
        ttk.Button(btn, text="확인", command=self._on_ok, width=10).pack(side="right", padx=4)

    def _on_ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("입력 필요", "규칙 이름을 입력하세요.", parent=self)
            return
        try:
            up = float(self.up_var.get())
            dn = float(self.dn_var.get())
        except ValueError:
            messagebox.showerror("입력 오류", "제한 값은 숫자로 입력하세요.", parent=self)
            return

        # 시간 형식 자동 정규화 + 검증
        start_norm = _normalize_hhmm(self.start_var.get())
        end_norm = _normalize_hhmm(self.end_var.get())
        if start_norm is None:
            messagebox.showerror(
                "시간 형식 오류",
                f"시작 시각이 올바르지 않습니다: '{self.start_var.get()}'\n\n"
                "사용 가능한 형식:\n"
                "  08:00  /  08  /  0800  /  8:00",
                parent=self,
            )
            return
        if end_norm is None:
            messagebox.showerror(
                "시간 형식 오류",
                f"종료 시각이 올바르지 않습니다: '{self.end_var.get()}'",
                parent=self,
            )
            return

        weekdays = [k for k, v in self.wd_vars.items() if v.get()]
        self.result = {
            "name": name,
            "time_start": start_norm,
            "time_end": end_norm,
            "weekdays": weekdays,
            "upload_limit_mbps": up,
            "download_limit_mbps": dn,
        }
        self.destroy()


def _normalize_hhmm(s: str) -> Optional[str]:
    """사용자 입력 시각을 HH:MM 형식으로 정규화.

    허용 입력:
        "08:00", "8:00", "08", "8", "0800", "800"
    반환: "08:00" 형식 or None (실패)
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None

    # "HH:MM" 혹은 "H:M" 형식
    if ":" in s:
        parts = s.split(":", 1)
        try:
            h = int(parts[0])
            m = int(parts[1]) if parts[1] else 0
        except ValueError:
            return None
    else:
        # 콜론 없음 → 숫자만
        if not s.isdigit():
            return None
        if len(s) <= 2:
            # "8", "08" → 8시 0분
            h = int(s)
            m = 0
        elif len(s) == 3:
            # "800" → 8시 0분
            h = int(s[0])
            m = int(s[1:])
        elif len(s) == 4:
            # "0800", "1800" → HH:MM
            h = int(s[:2])
            m = int(s[2:])
        else:
            return None

    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


# ──────────────────────────────────────────────────────────
# 예약 작업 관리 대화상자
# ──────────────────────────────────────────────────────────

class SchedulerDialog(tk.Toplevel):
    """OS 예약 작업 관리 대화상자."""

    def __init__(self, parent: tk.Tk):
        super().__init__(parent)
        self.title("예약 작업 관리")
        self.geometry("820x520")
        self.minsize(700, 420)
        self.transient(parent)
        self.grab_set()

        self.config_jobs: list[dict] = []
        self._load_jobs()
        self._build_ui()
        self._refresh_list()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _load_jobs(self) -> None:
        try:
            cfg = load_config()
            for j in cfg.scheduler.jobs:
                self.config_jobs.append({
                    "name": j.name,
                    "type": j.type or "",
                    "time": j.time or "",
                    "weekdays": list(j.weekdays),
                    "minute": j.minute,
                    "interval_minutes": j.interval_minutes,
                    "cron": j.cron or "",
                    "options": j.options or "",
                })
        except Exception:
            pass

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        # OS 등록 상태
        os_frame = ttk.LabelFrame(main, text="  OS 등록 상태  ", padding=8)
        os_frame.pack(fill="x", pady=(0, 8))

        self.os_status_var = tk.StringVar(value="확인 중...")
        ttk.Label(os_frame, textvariable=self.os_status_var, wraplength=700).pack(anchor="w")
        self._refresh_os_status()

        # Config jobs Treeview
        ttk.Label(main, text="config.yaml 예약 작업 목록:").pack(anchor="w", pady=(4, 4))

        tree_frame = ttk.Frame(main)
        tree_frame.pack(fill="both", expand=True)

        columns = ("name", "type", "time", "weekdays", "options")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8)
        self.tree.heading("name", text="이름")
        self.tree.heading("type", text="유형")
        self.tree.heading("time", text="시각")
        self.tree.heading("weekdays", text="요일")
        self.tree.heading("options", text="옵션")

        self.tree.column("name", width=140)
        self.tree.column("type", width=100)
        self.tree.column("time", width=80, anchor="center")
        self.tree.column("weekdays", width=180)
        self.tree.column("options", width=200)

        ysb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        # 버튼
        op_frame = ttk.Frame(main)
        op_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(op_frame, text="+ 추가", command=self._on_add, width=10).pack(side="left", padx=(0, 4))
        ttk.Button(op_frame, text="− 제거", command=self._on_remove, width=10).pack(side="left", padx=4)
        ttk.Button(op_frame, text="OS에 등록", command=self._on_register, width=12).pack(side="left", padx=4)
        ttk.Button(op_frame, text="OS에서 해제", command=self._on_unregister, width=12).pack(side="left", padx=4)
        ttk.Button(op_frame, text="전체 등록", command=self._on_register_all, width=12).pack(side="left", padx=4)

        save_frame = ttk.Frame(main)
        save_frame.pack(fill="x", pady=(10, 0))
        ttk.Button(save_frame, text="닫기", command=self.destroy, width=10).pack(side="right", padx=(4, 0))
        ttk.Button(save_frame, text="저장", command=self._on_save, width=10).pack(side="right", padx=4)

    def _refresh_os_status(self) -> None:
        try:
            from gdrive_sync.scheduler import get_scheduler
            sched = get_scheduler()
            jobs = sched.list_jobs()
            if jobs:
                self.os_status_var.set(f"등록된 작업 {len(jobs)}개: " + ", ".join(jobs))
            else:
                self.os_status_var.set("등록된 OS 작업 없음")
        except Exception as e:
            self.os_status_var.set(f"OS 작업 조회 실패: {e}")

    def _refresh_list(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        _WD_KR = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}
        for i, j in enumerate(self.config_jobs):
            jtype = j.get("type") or j.get("cron") or ""
            jtime = j.get("time") or (f"매 {j.get('minute', 0)}분" if jtype == "hourly" else "")
            if jtype == "interval":
                jtime = f"매 {j.get('interval_minutes', 0)}분"
            wd = ", ".join(_WD_KR.get(d, d) for d in j.get("weekdays", [])) if j.get("weekdays") else ""
            self.tree.insert("", "end", iid=str(i), values=(
                j["name"], jtype, jtime, wd, j.get("options", ""),
            ))

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except ValueError:
            return None

    def _on_add(self) -> None:
        dlg = SchedulerJobEditDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            self.config_jobs.append(dlg.result)
            self._refresh_list()

    def _on_remove(self) -> None:
        idx = self._selected_index()
        if idx is None:
            return
        name = self.config_jobs[idx]["name"]
        if not messagebox.askyesno("제거 확인", f"'{name}' 작업을 목록에서 제거하시겠습니까?", parent=self):
            return
        del self.config_jobs[idx]
        self._refresh_list()

    def _on_register(self) -> None:
        """선택한 작업을 OS에 등록."""
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("알림", "등록할 작업을 선택하세요.", parent=self)
            return
        j = self.config_jobs[idx]
        try:
            from gdrive_sync.scheduler import get_scheduler, default_python_executable
            job = SchedulerJob(
                name=j["name"], type=j.get("type") or None, time=j.get("time") or None,
                weekdays=j.get("weekdays", []), minute=j.get("minute"),
                interval_minutes=j.get("interval_minutes"),
                cron=j.get("cron") or None, options=j.get("options", ""),
            )
            sched = get_scheduler()
            python_exe = default_python_executable()
            result = sched.register(job, python_exe)
            messagebox.showinfo("등록 완료", f"OS 등록 완료:\n{result}", parent=self)
            self._refresh_os_status()
        except Exception as e:
            messagebox.showerror("등록 실패", f"OS 등록 실패:\n{e}", parent=self)

    def _on_unregister(self) -> None:
        """선택한 작업을 OS에서 해제."""
        idx = self._selected_index()
        if idx is None:
            messagebox.showinfo("알림", "해제할 작업을 선택하세요.", parent=self)
            return
        name = self.config_jobs[idx]["name"]
        try:
            from gdrive_sync.scheduler import get_scheduler
            sched = get_scheduler()
            if sched.unregister(name):
                messagebox.showinfo("해제 완료", f"'{name}' OS 등록 해제 완료.", parent=self)
            else:
                messagebox.showinfo("알림", f"'{name}'은 OS에 등록되어 있지 않습니다.", parent=self)
            self._refresh_os_status()
        except Exception as e:
            messagebox.showerror("해제 실패", f"OS 해제 실패:\n{e}", parent=self)

    def _on_register_all(self) -> None:
        """모든 config 작업을 OS에 등록."""
        if not self.config_jobs:
            messagebox.showinfo("알림", "등록할 작업이 없습니다.", parent=self)
            return
        try:
            from gdrive_sync.scheduler import get_scheduler, default_python_executable
            sched = get_scheduler()
            python_exe = default_python_executable()
            ok = 0
            errors = []
            for j in self.config_jobs:
                try:
                    job = SchedulerJob(
                        name=j["name"], type=j.get("type") or None, time=j.get("time") or None,
                        weekdays=j.get("weekdays", []), minute=j.get("minute"),
                        interval_minutes=j.get("interval_minutes"),
                        cron=j.get("cron") or None, options=j.get("options", ""),
                    )
                    sched.register(job, python_exe)
                    ok += 1
                except Exception as e:
                    errors.append(f"{j['name']}: {e}")

            msg = f"{ok}개 작업 등록 완료."
            if errors:
                msg += f"\n\n오류 {len(errors)}개:\n" + "\n".join(errors)
            messagebox.showinfo("전체 등록", msg, parent=self)
            self._refresh_os_status()
        except Exception as e:
            messagebox.showerror("등록 실패", f"전체 등록 실패:\n{e}", parent=self)

    def _on_save(self) -> None:
        """config.yaml에 scheduler.jobs 저장."""
        try:
            import yaml
            path = DEFAULT_CONFIG_PATH
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            else:
                raw = default_config_template()

            if "scheduler" not in raw:
                raw["scheduler"] = {}
            raw["scheduler"]["jobs"] = []
            for j in self.config_jobs:
                entry: dict = {"name": j["name"]}
                if j.get("type"):
                    entry["type"] = j["type"]
                if j.get("time"):
                    entry["time"] = j["time"]
                if j.get("weekdays"):
                    entry["weekdays"] = j["weekdays"]
                if j.get("minute") is not None:
                    entry["minute"] = j["minute"]
                if j.get("interval_minutes") is not None:
                    entry["interval_minutes"] = j["interval_minutes"]
                if j.get("cron"):
                    entry["cron"] = j["cron"]
                if j.get("options"):
                    entry["options"] = j["options"]
                raw["scheduler"]["jobs"].append(entry)

            save_config(raw, path)
            messagebox.showinfo("저장 완료", f"{len(self.config_jobs)}개 예약 작업이 저장되었습니다.", parent=self)
            self.destroy()
        except Exception as e:
            messagebox.showerror("저장 실패", f"설정 저장 실패:\n{e}", parent=self)


class SchedulerJobEditDialog(tk.Toplevel):
    """예약 작업 한 개 입력."""

    _WEEKDAYS = [("mon", "월"), ("tue", "화"), ("wed", "수"), ("thu", "목"),
                 ("fri", "금"), ("sat", "토"), ("sun", "일")]
    _TYPES = ["daily", "weekly", "hourly", "interval"]

    def __init__(self, parent: tk.Toplevel, initial: Optional[dict] = None):
        super().__init__(parent)
        self.title("예약 작업 편집")
        self.geometry("500x360")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: Optional[dict] = None
        initial = initial or {
            "name": "", "type": "daily", "time": "12:00",
            "weekdays": [], "minute": None, "interval_minutes": None,
            "cron": "", "options": "",
        }
        self._build_ui(initial)

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.bind("<Escape>", lambda e: self.destroy())

    def _build_ui(self, initial: dict) -> None:
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)

        # 이름
        ttk.Label(main, text="작업 이름:").grid(row=0, column=0, sticky="w", pady=4)
        self.name_var = tk.StringVar(value=initial["name"])
        ttk.Entry(main, textvariable=self.name_var, width=25).grid(row=0, column=1, columnspan=3, sticky="w", pady=4)

        # 유형
        ttk.Label(main, text="유형:").grid(row=1, column=0, sticky="w", pady=4)
        self.type_var = tk.StringVar(value=initial.get("type") or "daily")
        type_combo = ttk.Combobox(main, textvariable=self.type_var, values=self._TYPES, state="readonly", width=12)
        type_combo.grid(row=1, column=1, sticky="w", pady=4)

        # 시각
        ttk.Label(main, text="시각 (HH:MM):").grid(row=2, column=0, sticky="w", pady=4)
        self.time_var = tk.StringVar(value=initial.get("time") or "12:00")
        ttk.Entry(main, textvariable=self.time_var, width=8).grid(row=2, column=1, sticky="w", pady=4)

        # 분 (hourly)
        ttk.Label(main, text="분 (hourly용):").grid(row=2, column=2, sticky="w", pady=4, padx=(10, 0))
        self.minute_var = tk.StringVar(value=str(initial.get("minute") or ""))
        ttk.Entry(main, textvariable=self.minute_var, width=6).grid(row=2, column=3, sticky="w", pady=4)

        # interval
        ttk.Label(main, text="간격 (분, interval용):").grid(row=3, column=0, sticky="w", pady=4)
        self.interval_var = tk.StringVar(value=str(initial.get("interval_minutes") or ""))
        ttk.Entry(main, textvariable=self.interval_var, width=8).grid(row=3, column=1, sticky="w", pady=4)

        # 요일
        ttk.Label(main, text="요일 (weekly용):").grid(row=4, column=0, sticky="w", pady=4)
        wd_frame = ttk.Frame(main)
        wd_frame.grid(row=4, column=1, columnspan=3, sticky="w", pady=4)

        self.wd_vars: dict[str, tk.BooleanVar] = {}
        existing_wd = set(initial.get("weekdays", []))
        for key, label in self._WEEKDAYS:
            var = tk.BooleanVar(value=(key in existing_wd))
            self.wd_vars[key] = var
            ttk.Checkbutton(wd_frame, text=label, variable=var).pack(side="left", padx=2)

        # 옵션
        ttk.Label(main, text="추가 옵션:").grid(row=5, column=0, sticky="w", pady=4)
        self.options_var = tk.StringVar(value=initial.get("options", ""))
        ttk.Entry(main, textvariable=self.options_var, width=30).grid(row=5, column=1, columnspan=3, sticky="w", pady=4)

        ttk.Label(main, text="예: --no-limit  --dry-run", foreground="gray").grid(
            row=6, column=0, columnspan=4, sticky="w", pady=(0, 8))

        # 버튼
        btn = ttk.Frame(main)
        btn.grid(row=7, column=0, columnspan=4, sticky="e", pady=(10, 0))
        ttk.Button(btn, text="취소", command=self.destroy, width=10).pack(side="right", padx=(4, 0))
        ttk.Button(btn, text="확인", command=self._on_ok, width=10).pack(side="right", padx=4)

    def _on_ok(self) -> None:
        name = self.name_var.get().strip()
        if not name:
            messagebox.showwarning("입력 필요", "작업 이름을 입력하세요.", parent=self)
            return

        jtype = self.type_var.get()
        weekdays = [k for k, v in self.wd_vars.items() if v.get()]

        minute_val = None
        if self.minute_var.get().strip():
            try:
                minute_val = int(self.minute_var.get().strip())
            except ValueError:
                messagebox.showerror("입력 오류", "분 값은 숫자로 입력하세요.", parent=self)
                return

        interval_val = None
        if self.interval_var.get().strip():
            try:
                interval_val = int(self.interval_var.get().strip())
            except ValueError:
                messagebox.showerror("입력 오류", "간격 값은 숫자로 입력하세요.", parent=self)
                return

        # 시간 필드 정규화 (daily/weekly에서 사용)
        time_val = self.time_var.get().strip()
        if jtype in ("daily", "weekly") and time_val:
            normalized = _normalize_hhmm(time_val)
            if normalized is None:
                messagebox.showerror(
                    "시간 형식 오류",
                    f"시각이 올바르지 않습니다: '{time_val}'\n\n"
                    "사용 가능한 형식: 08:00 / 0800 / 8:00 / 08",
                    parent=self,
                )
                return
            time_val = normalized

        self.result = {
            "name": name,
            "type": jtype,
            "time": time_val,
            "weekdays": weekdays,
            "minute": minute_val,
            "interval_minutes": interval_val,
            "cron": "",
            "options": self.options_var.get().strip(),
        }
        self.destroy()


# ──────────────────────────────────────────────────────────
# 한글 파일명 정규화 대화상자 (NFD → NFC)
# ──────────────────────────────────────────────────────────

class NormalizeDialog(tk.Toplevel):
    """동기화 폴더의 한글 분절 파일명을 일괄 NFC 정규화."""

    def __init__(self, parent: tk.Tk, sync_pairs, on_log=None):
        super().__init__(parent)
        self.title("한글 파일명 일괄 정규화 (NFD → NFC)")
        self.geometry("760x560")
        self.minsize(620, 460)
        self.transient(parent)
        self.grab_set()

        self.sync_pairs = list(sync_pairs)
        self.on_log = on_log
        self._worker: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._msg_queue: queue.Queue = queue.Queue()

        self._build_ui()

        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)

    def _build_ui(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        # 설명
        info = (
            "macOS에서 만들어진 파일이 Windows에서 한글이 분절돼 보이는 문제(NFD → NFC)를\n"
            "해결합니다. 아래 폴더의 모든 파일/하위폴더 이름이 완성형 한글로 변환됩니다.\n"
            "먼저 [미리보기]로 어떤 파일이 바뀔지 확인한 뒤 [실행]을 누르세요."
        )
        ttk.Label(main, text=info, justify="left", foreground="#444").pack(
            anchor="w", pady=(0, 8)
        )

        # 폴더 체크리스트
        list_frame = ttk.LabelFrame(main, text="  대상 폴더  ", padding=8)
        list_frame.pack(fill="x", pady=(0, 8))

        self._target_vars: list[tuple[tk.BooleanVar, Path]] = []
        for pair in self.sync_pairs:
            var = tk.BooleanVar(value=True)
            cb = ttk.Checkbutton(
                list_frame, text=str(pair.local_path), variable=var
            )
            cb.pack(anchor="w")
            self._target_vars.append((var, pair.local_path))

        # 전체 선택/해제
        toggle_frame = ttk.Frame(list_frame)
        toggle_frame.pack(anchor="w", pady=(4, 0))
        ttk.Button(
            toggle_frame, text="전체 선택",
            command=lambda: [v.set(True) for v, _ in self._target_vars],
            width=10,
        ).pack(side="left", padx=(0, 4))
        ttk.Button(
            toggle_frame, text="전체 해제",
            command=lambda: [v.set(False) for v, _ in self._target_vars],
            width=10,
        ).pack(side="left")

        # 진행 라벨
        self.status_var = tk.StringVar(value="준비됨")
        ttk.Label(main, textvariable=self.status_var, foreground="#0066CC").pack(
            anchor="w", pady=(0, 4)
        )

        # 결과 로그
        log_frame = ttk.LabelFrame(main, text="  결과  ", padding=4)
        log_frame.pack(fill="both", expand=True, pady=(0, 8))

        self.text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        ysb = ttk.Scrollbar(log_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=ysb.set)
        ysb.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        # 색상 태그
        self.text.tag_configure("info", foreground="#333")
        self.text.tag_configure("ok", foreground="#0a8a0a")
        self.text.tag_configure("warn", foreground="#b06b00")
        self.text.tag_configure("err", foreground="#c00")
        self.text.tag_configure("title", font=("Segoe UI", 10, "bold"))

        # 버튼
        btn_frame = ttk.Frame(main)
        btn_frame.pack(fill="x")
        self.preview_btn = ttk.Button(
            btn_frame, text="미리보기 (변경 없음)", width=20,
            command=lambda: self._start(dry_run=True),
        )
        self.preview_btn.pack(side="left", padx=(0, 4))
        self.run_btn = ttk.Button(
            btn_frame, text="실행 (이름 변경)", width=18,
            command=lambda: self._start(dry_run=False),
        )
        self.run_btn.pack(side="left", padx=(0, 4))
        self.cancel_btn = ttk.Button(
            btn_frame, text="중단", width=10, state="disabled",
            command=self._cancel.set,
        )
        self.cancel_btn.pack(side="left")
        ttk.Button(btn_frame, text="닫기", command=self._on_close, width=10).pack(side="right")

    def _append(self, text: str, tag: str = "info") -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text + "\n", tag)
        self.text.see("end")
        self.text.configure(state="disabled")

    def _start(self, dry_run: bool) -> None:
        if self._worker and self._worker.is_alive():
            return
        targets = [p for v, p in self._target_vars if v.get()]
        if not targets:
            messagebox.showinfo("알림", "대상 폴더를 1개 이상 선택하세요.", parent=self)
            return

        if not dry_run:
            if not messagebox.askyesno(
                "실행 확인",
                "선택한 폴더의 분절된 한글 파일명을 모두 변경합니다.\n"
                "되돌리려면 다시 정규화 도구를 사용해야 합니다.\n\n계속하시겠습니까?",
                parent=self,
            ):
                return

        # UI 잠금
        self.preview_btn.configure(state="disabled")
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self._cancel.clear()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._append(
            f"=== {'미리보기' if dry_run else '실행'} 시작 ({len(targets)}개 폴더) ===",
            "title",
        )

        self._worker = threading.Thread(
            target=self._run, args=(targets, dry_run), daemon=True
        )
        self._worker.start()

    def _run(self, targets: list[Path], dry_run: bool) -> None:
        from gdrive_sync.normalize import normalize_path

        total_scan = total_fix = total_renamed = total_conflict = total_err = 0
        try:
            for t in targets:
                if self._cancel.is_set():
                    self._msg_queue.put(("warn", "사용자에 의해 중단됨"))
                    break
                self._msg_queue.put(("status", f"진행 중: {t}"))
                self._msg_queue.put(("title", f"\n→ {t}"))

                def _prog(_p, _action, _q=self._msg_queue, _cancel=self._cancel):
                    if _cancel.is_set():
                        raise KeyboardInterrupt()

                try:
                    rep = normalize_path(t, dry_run=dry_run, progress=_prog)
                except KeyboardInterrupt:
                    self._msg_queue.put(("warn", "  (취소됨)"))
                    break

                total_scan += rep.scanned
                total_fix += rep.needs_fix
                total_renamed += rep.renamed
                total_conflict += rep.skipped_conflict
                total_err += rep.errors

                self._msg_queue.put((
                    "info",
                    f"  검사 {rep.scanned} / 정규화 필요 {rep.needs_fix} / "
                    f"변경 {rep.renamed} / 충돌 {rep.skipped_conflict} / 오류 {rep.errors}",
                ))
                # 변경 예시 (최대 10건)
                shown = rep.changes[:10]
                for src, dst in shown:
                    self._msg_queue.put(("ok", f"    · {src.name}  →  {dst.name}"))
                if len(rep.changes) > len(shown):
                    self._msg_queue.put(
                        ("info", f"    · ...외 {len(rep.changes) - len(shown)}건")
                    )
                for src in rep.conflicts[:5]:
                    self._msg_queue.put(("warn", f"    ! 충돌(스킵): {src}"))
                for src, msg in rep.error_paths[:5]:
                    self._msg_queue.put(("err", f"    ✕ 오류: {src} ({msg})"))

            # 합계
            self._msg_queue.put(("title", "\n=== 합계 ==="))
            self._msg_queue.put((
                "info",
                f"검사 {total_scan} / 정규화 필요 {total_fix} / 변경 {total_renamed} "
                f"/ 충돌 {total_conflict} / 오류 {total_err}",
            ))
            if dry_run and total_fix > 0:
                self._msg_queue.put((
                    "warn",
                    "→ 미리보기 모드입니다. 실제 변경하려면 [실행]을 누르세요.",
                ))
            elif total_renamed > 0:
                self._msg_queue.put(("ok", "→ 정규화 완료."))
            elif total_fix == 0:
                self._msg_queue.put(("ok", "→ 분절된 파일명이 없습니다. 정상."))

            if self.on_log:
                try:
                    self.on_log(
                        f"파일명 정규화 {'미리보기' if dry_run else '실행'}: "
                        f"검사 {total_scan} / 변경 {total_renamed} / 오류 {total_err}",
                        "SUCCESS" if total_err == 0 else "WARN",
                    )
                except Exception:
                    pass
        except Exception as e:
            self._msg_queue.put(("err", f"예외: {e}"))
        finally:
            self._msg_queue.put(("done", ""))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self._msg_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(payload)
                elif kind == "done":
                    self.status_var.set("완료")
                    self.preview_btn.configure(state="normal")
                    self.run_btn.configure(state="normal")
                    self.cancel_btn.configure(state="disabled")
                else:
                    tag = kind if kind in ("info", "ok", "warn", "err", "title") else "info"
                    self._append(payload, tag)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._drain_queue)

    def _on_close(self) -> None:
        if self._worker and self._worker.is_alive():
            if not messagebox.askyesno(
                "확인", "정규화 작업이 진행 중입니다. 중단하고 닫을까요?",
                parent=self,
            ):
                return
            self._cancel.set()
        self.destroy()


# ──────────────────────────────────────────────────────────
# 엔트리포인트
# ──────────────────────────────────────────────────────────

def run_gui() -> None:
    """GUI 실행 엔트리. cli.gui 명령에서 호출."""
    root = tk.Tk()
    app = SyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
