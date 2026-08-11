"""gdrive-sync CLI (click 기반).

v2 추가 옵션/명령:
- sync: --parallel N / --upload-limit / --download-limit / --no-limit
- schedule list / add / remove / install-from-config
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import click

from gdrive_sync import __version__
from gdrive_sync.auth import (
    DEFAULT_CREDENTIALS_PATH, DEFAULT_TOKEN_PATH,
    load_credentials, revoke_token,
)
from gdrive_sync.bandwidth import BandwidthLimiter, make_limiter
from gdrive_sync.config import (
    DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH,
    BandwidthConfig, Config, NetworkConfig, SchedulerJob,
    default_config_template, load_config, save_config,
)
from gdrive_sync.network import test_connections
from gdrive_sync.state import clear_state, load_state
from gdrive_sync.sync_engine import (
    ActionType, SyncEngine, SyncSummary, format_vanished_samples,
)
from gdrive_sync.utils import human_size


# ──────────────────────────────────────────────────────────
# 로깅 초기화
# ──────────────────────────────────────────────────────────

def _setup_logging(cfg: Optional[Config] = None, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if cfg:
        level = getattr(logging, cfg.log_level.upper(), level)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if cfg and cfg.log_file:
        log_path = DEFAULT_CONFIG_DIR / cfg.log_file
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=9, encoding="utf-8"
        )
        handlers.append(fh)

    # Windows 콘솔 UTF-8
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def _load_cfg_or_exit(path: Optional[Path] = None) -> Config:
    try:
        return load_config(path)
    except FileNotFoundError as e:
        click.secho(str(e), fg="red", err=True)
        sys.exit(2)


# ──────────────────────────────────────────────────────────
# 메인 그룹
# ──────────────────────────────────────────────────────────

@click.group()
@click.version_option(__version__, prog_name="gdrive-sync")
@click.option("--config", "-c", "config_path", type=click.Path(path_type=Path),
              help="설정 파일 경로 (기본: ~/.gdrive_sync/config.yaml)")
@click.option("--verbose", "-v", is_flag=True, help="디버그 로그 출력")
@click.pass_context
def main(ctx: click.Context, config_path: Optional[Path], verbose: bool):
    """크로스플랫폼 구글드라이브 양방향 동기화 CLI."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose


# ──────────────────────────────────────────────────────────
# init
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--force", is_flag=True, help="기존 설정 덮어쓰기")
@click.pass_context
def init(ctx, force: bool):
    """설정 파일 초기화 (대화형)."""
    _setup_logging(verbose=ctx.obj["verbose"])
    cfg_path: Path = ctx.obj["config_path"] or DEFAULT_CONFIG_PATH

    if cfg_path.exists() and not force:
        click.secho(f"이미 설정이 존재합니다: {cfg_path}", fg="yellow")
        if not click.confirm("덮어쓰시겠습니까?", default=False):
            return

    template = default_config_template()

    click.secho("\n=== gdrive-sync 초기 설정 ===\n", fg="cyan", bold=True)

    local = click.prompt(
        "동기화할 로컬 폴더 경로",
        default="~/GDriveSync",
    )
    remote = click.prompt(
        "구글드라이브 상의 폴더 이름 또는 경로",
        default="동기화테스트",
    )
    template["sync_pairs"] = [{"local_path": local, "remote_path": remote}]

    policy_map = {"1": "newer_wins", "2": "local_wins", "3": "remote_wins", "4": "keep_both"}
    click.echo("\n충돌 해결 정책:")
    click.echo("  1) newer_wins  - 최신 수정시간 우선 (추천)")
    click.echo("  2) local_wins  - 항상 로컬 우선")
    click.echo("  3) remote_wins - 항상 리모트 우선")
    click.echo("  4) keep_both   - 양쪽 모두 보존")
    choice = click.prompt("선택", default="1", type=click.Choice(list(policy_map)))
    template["conflict_policy"] = policy_map[choice]

    delete_map = {"1": "trash", "2": "permanent", "3": "skip"}
    click.echo("\n삭제 정책:")
    click.echo("  1) trash     - 휴지통 이동 (추천)")
    click.echo("  2) permanent - 즉시 삭제")
    click.echo("  3) skip      - 삭제 전파 안 함")
    choice = click.prompt("선택", default="1", type=click.Choice(list(delete_map)))
    template["delete_policy"] = delete_map[choice]

    parallel = click.prompt(
        "동시 전송 파일 수 (1~10, 추천 5)",
        default=5, type=click.IntRange(1, 10),
    )
    template["performance"]["parallel_transfers"] = parallel

    saved = save_config(template, cfg_path)
    click.secho(f"\n✓ 설정 저장: {saved}", fg="green")
    click.echo("\n다음 단계:")
    click.echo(f"  1) credentials.json 파일을 {Path.home() / '.gdrive_sync'} 에 배치")
    click.echo("  2) gdrive-sync auth  (최초 인증)")
    click.echo("  3) gdrive-sync test-connection")
    click.echo("  4) gdrive-sync sync --dry-run")


# ──────────────────────────────────────────────────────────
# auth
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--credentials", "-k", type=click.Path(path_type=Path),
              default=DEFAULT_CREDENTIALS_PATH,
              help="credentials.json 경로")
@click.option("--revoke", is_flag=True, help="기존 토큰 삭제 (재인증 강제)")
@click.pass_context
def auth(ctx, credentials: Path, revoke: bool):
    """Google OAuth 인증 수행."""
    _setup_logging(verbose=ctx.obj["verbose"])

    if revoke:
        if revoke_token():
            click.secho("기존 토큰 삭제 완료", fg="yellow")

    try:
        creds = load_credentials(
            credentials_path=credentials,
            interactive=True,
        )
        click.secho("✓ 인증 성공", fg="green")
        click.echo(f"  토큰: {DEFAULT_TOKEN_PATH}")
        click.echo(f"  유효: {creds.valid}")
    except Exception as e:
        click.secho(f"인증 실패: {e}", fg="red", err=True)
        sys.exit(1)


# ──────────────────────────────────────────────────────────
# test-connection
# ──────────────────────────────────────────────────────────

@main.command("test-connection")
@click.pass_context
def test_connection(ctx):
    """네트워크 연결 테스트."""
    _setup_logging(verbose=ctx.obj["verbose"])

    try:
        cfg = load_config(ctx.obj["config_path"])
        net = cfg.network
    except FileNotFoundError:
        net = NetworkConfig()

    click.secho("\n=== 구글드라이브 연결 테스트 ===\n", fg="cyan", bold=True)
    results = test_connections(net)

    max_method = max((len(r.method) for r in results), default=20)
    for r in results:
        icon = click.style("✓", fg="green") if r.ok else click.style("✕", fg="red")
        elapsed = f"{r.elapsed_ms}ms" if r.elapsed_ms else ""
        click.echo(f"  {icon}  {r.method:<{max_method}}  {elapsed:>8}  {r.detail}")

    direct_ok = any(r.method.startswith("direct") and r.ok for r in results)
    if direct_ok:
        click.secho("\n→ 직접 연결 가능. 프록시 불필요.", fg="green")
    else:
        click.secho("\n→ 직접 연결 불가. config.yaml의 network 섹션을 확인하세요.", fg="yellow")


# ──────────────────────────────────────────────────────────
# sync
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--dry-run", is_flag=True, help="실제 전송 없이 변경사항만 미리보기")
@click.option("--force-upload", is_flag=True, help="로컬 → 리모트 강제 업로드")
@click.option("--force-download", is_flag=True, help="리모트 → 로컬 강제 다운로드")
@click.option("--parallel", "parallel_override", type=click.IntRange(1, 10),
              help="동시 전송 수 (1~10). config.yaml 값 오버라이드")
@click.option("--upload-limit", type=float, default=None,
              help="업로드 속도 제한 (MB/s). 0=무제한")
@click.option("--download-limit", type=float, default=None,
              help="다운로드 속도 제한 (MB/s). 0=무제한")
@click.option("--no-limit", is_flag=True,
              help="모든 대역폭 제한 해제 (config/schedule 무시)")
@click.pass_context
def sync(
    ctx, dry_run, force_upload, force_download,
    parallel_override, upload_limit, download_limit, no_limit,
):
    """동기화 실행."""
    if force_upload and force_download:
        click.secho("--force-upload 와 --force-download 를 동시 사용할 수 없습니다.",
                    fg="red", err=True)
        sys.exit(2)

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    # CLI 대역폭 옵션 → config 덮어쓰기
    bw_cfg = cfg.bandwidth
    if no_limit:
        bw_cfg = BandwidthConfig(enabled=False)
    elif upload_limit is not None or download_limit is not None:
        bw_cfg = BandwidthConfig(
            enabled=True,
            upload_limit_mbps=upload_limit if upload_limit is not None else bw_cfg.upload_limit_mbps,
            download_limit_mbps=download_limit if download_limit is not None else bw_cfg.download_limit_mbps,
            schedule=bw_cfg.schedule,
        )

    limiter = make_limiter(bw_cfg)
    if limiter:
        st = limiter.get_status()
        click.secho(
            f"대역폭 제한: rule={st['active_rule']} / "
            f"up={st['upload_mbps'] or '무제한'} MB/s / "
            f"dn={st['download_mbps'] or '무제한'} MB/s",
            fg="yellow",
        )

    from gdrive_sync.drive_api import DriveClient
    try:
        drive = DriveClient(
            cfg.network,
            interactive_auth=False,
            performance=cfg.performance,
        )
    except Exception as e:
        click.secho(f"Drive 클라이언트 초기화 실패: {e}", fg="red", err=True)
        sys.exit(1)

    force_mode = None
    if force_upload:
        force_mode = "upload"
    elif force_download:
        force_mode = "download"

    # tqdm 프로그래스 팩토리
    from tqdm import tqdm

    _progress_pos = {"next": 0}
    def progress_factory(label: str, total: int, arrow: str):
        short = label if len(label) < 40 else "..." + label[-37:]
        pos = _progress_pos["next"]
        _progress_pos["next"] += 1
        return tqdm(
            total=total, unit="B", unit_scale=True,
            desc=f"{arrow} {short}",
            leave=False,
            position=pos,
        )

    engine = SyncEngine(
        cfg=cfg,
        drive=drive,
        dry_run=dry_run,
        force_mode=force_mode,
        progress_factory=None if dry_run else progress_factory,
        bandwidth_limiter=limiter,
        parallel_override=parallel_override,
    )

    start = time.time()
    try:
        results = engine.run()
    except KeyboardInterrupt:
        click.secho("\n사용자 중단", fg="yellow")
        sys.exit(130)
    elapsed = time.time() - start

    _print_summary(results, elapsed, dry_run)

    if not dry_run:
        _maybe_print_update_hint()


def _maybe_print_update_hint() -> None:
    """스로틀된 새 버전 확인 — 하루 1회만 네트워크 조회, 실패는 조용히 무시."""
    try:
        from gdrive_sync.update_check import check_for_update
        info = check_for_update(timeout=3.0)
        if info and info.available:
            click.secho(
                f"\n새 버전 v{info.latest} 이(가) 있습니다 (현재 v{info.current}). "
                f"'gdrive-sync update' 로 업데이트하세요.",
                fg="cyan",
            )
    except Exception:
        pass


def _print_summary(results, elapsed: float, dry_run: bool) -> None:
    click.echo()
    click.secho("=== 동기화 완료 ===" if not dry_run else "=== DRY-RUN 결과 ===",
                fg="cyan", bold=True)

    total = SyncSummary()
    for pair, s in results:
        total.uploaded += s.uploaded
        total.uploaded_bytes += s.uploaded_bytes
        total.downloaded += s.downloaded
        total.downloaded_bytes += s.downloaded_bytes
        total.deleted_local += s.deleted_local
        total.deleted_remote += s.deleted_remote
        total.pruned_dirs += getattr(s, "pruned_dirs", 0)
        total.moved_remote += getattr(s, "moved_remote", 0)
        total.moved_local += getattr(s, "moved_local", 0)
        total.pruned_remote_dirs += getattr(s, "pruned_remote_dirs", 0)
        total.normalized_remote += getattr(s, "normalized_remote", 0)
        total.conflicts += s.conflicts
        total.skipped += s.skipped
        total.errors += s.errors
        total.vanished += getattr(s, "vanished", 0)
        for name, cnt in getattr(s, "vanished_samples", {}).items():
            total.vanished_samples[name] = total.vanished_samples.get(name, 0) + cnt

    if dry_run:
        counts: dict[str, int] = {}
        for _, s in results:
            for a in s.actions:
                counts[a.type.value] = counts.get(a.type.value, 0) + 1
        for name, cnt in sorted(counts.items()):
            click.echo(f"  {name:<20}  {cnt}개")
    else:
        click.echo(f"  ↑ 업로드    {total.uploaded:>4}개 ({human_size(total.uploaded_bytes)})")
        click.echo(f"  ↓ 다운로드  {total.downloaded:>4}개 ({human_size(total.downloaded_bytes)})")
        click.echo(f"  ✕ 로컬삭제  {total.deleted_local:>4}개")
        click.echo(f"  ✕ 리모트삭제 {total.deleted_remote:>4}개")
        if total.moved_remote or total.moved_local:
            click.echo(
                f"  ➜ 이동감지  {total.moved_remote + total.moved_local:>4}개 "
                f"(Drive {total.moved_remote} / 로컬 {total.moved_local} — 재전송 생략)"
            )
        if total.pruned_dirs:
            click.echo(f"  🗑 빈폴더정리 {total.pruned_dirs:>3}개")
        if total.pruned_remote_dirs:
            click.echo(f"  🗑 Drive빈폴더 {total.pruned_remote_dirs:>2}개")
        if total.normalized_remote:
            click.echo(f"  🔤 Drive명정규화 {total.normalized_remote:>2}개 (NFD→NFC)")
        click.echo(f"  ⚡ 충돌     {total.conflicts:>4}개")
        click.echo(f"  ⏭ 건너뜀   {total.skipped:>4}개")
        if total.errors:
            click.secho(f"  ⚠ 오류     {total.errors:>4}개", fg="red")
        if total.vanished:
            click.secho(
                f"  ⊘ 사라진 파일 {total.vanished:>3}개  "
                f"(스캔 후 백신/DLP가 지운 임시파일 추정 — 정상)",
                fg="yellow",
            )
            samples = format_vanished_samples(total.vanished_samples)
            if samples:
                click.secho(f"     예: {samples}", fg="yellow")
        click.echo(f"  ⏱ 소요시간  {elapsed:.1f}초")


# ──────────────────────────────────────────────────────────
# status / config / reset-state (기존과 동일)
# ──────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def status(ctx):
    """마지막 동기화 상태 및 변경 요약."""
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    for pair in cfg.sync_pairs:
        click.secho(f"\n[{pair.local_path}] ↔ [{pair.remote_path}]", fg="cyan", bold=True)
        st = load_state(pair.local_path)
        click.echo(f"  마지막 동기화: {st.last_sync or '(없음)'}")
        click.echo(f"  추적 파일 수:  {len(st.files)}개")
        if not pair.local_path.exists():
            click.secho("  ⚠ 로컬 경로가 존재하지 않습니다", fg="yellow")

    # 대역폭 현재 상태
    limiter = make_limiter(cfg.bandwidth)
    if limiter:
        st = limiter.get_status()
        click.secho(f"\n[대역폭 제한] 활성: {st['active_rule']}", fg="cyan", bold=True)
        click.echo(f"  업로드:   {st['upload_mbps']} MB/s" if not st['upload_unlimited'] else "  업로드:   무제한")
        click.echo(f"  다운로드: {st['download_mbps']} MB/s" if not st['download_unlimited'] else "  다운로드: 무제한")


@main.command()
@click.argument("name")
@click.option("--exact", is_flag=True, help="이름 완전 일치 (기본: 부분 일치)")
@click.option("--no-trashed", is_flag=True, help="휴지통 항목 제외")
@click.option("--limit", default=50, show_default=True, help="최대 결과 수")
@click.pass_context
def diagnose(ctx, name: str, exact: bool, no_trashed: bool, limit: int):
    """Drive 에서 이름으로 항목 검색 — 핑퐁/ghost-cleanup 진단용.

    각 항목의 trashed/parent/owners/mtime 을 표시. 어느 PC가 trash로 보냈는지,
    여전히 살아있는지, 부모 경로가 어디인지 즉시 확인.

    예시:
      gdrive-sync diagnose 김광영
      gdrive-sync diagnose 감사제보 --no-trashed
      gdrive-sync diagnose "민원" --exact
    """
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    from gdrive_sync.drive_api import DriveClient
    try:
        drive = DriveClient(
            cfg.network,
            interactive_auth=False,
            performance=cfg.performance,
        )
    except Exception as e:
        click.secho(f"Drive 클라이언트 초기화 실패: {e}", fg="red", err=True)
        sys.exit(1)

    click.secho(
        f"\n🔍 Drive 검색: name {'==' if exact else 'contains'} '{name}'"
        f"{' (trashed 제외)' if no_trashed else ' (살아있음 + 휴지통 포함)'}",
        fg="cyan", bold=True,
    )
    try:
        hits = drive.search_by_name(
            name, exact=exact, include_trashed=not no_trashed, limit=limit,
        )
    except Exception as e:
        click.secho(f"검색 실패: {e}", fg="red", err=True)
        sys.exit(1)

    if not hits:
        click.secho("  매칭 결과 없음.", fg="yellow")
        return

    # trashed 와 살아있는 항목 분리해서 보기
    alive = [h for h in hits if not h.get("trashed")]
    trashed = [h for h in hits if h.get("trashed")]

    def _print(items: list[dict], header: str, color: str):
        if not items:
            return
        click.secho(f"\n{header} ({len(items)}개)", fg=color, bold=True)
        for h in items:
            kind = "📁" if h.get("mimeType") == "application/vnd.google-apps.folder" else "📄"
            owner = ""
            owners = h.get("owners") or []
            if owners:
                owner = owners[0].get("displayName") or owners[0].get("emailAddress") or ""
            owned_by_me = h.get("ownedByMe", True)
            owned_mark = "" if owned_by_me else " [공유자료]"
            explicit = " [명시적휴지통]" if h.get("explicitlyTrashed") else ""

            click.echo(
                f"  {kind} {h.get('name','?')}  id={h.get('id','?')}  "
                f"mtime={h.get('modifiedTime','?')}  "
                f"size={h.get('size','-')}  owner={owner}{owned_mark}{explicit}"
            )
            # 부모 경로 추적
            parents = h.get("parents") or []
            if parents:
                try:
                    path = drive.get_parent_path(parents[0])
                    click.echo(f"      └ parent: {path}")
                except Exception:
                    click.echo(f"      └ parent id: {parents[0]}")

    _print(alive, "✅ 살아있는 항목", "green")
    _print(trashed, "🗑 휴지통 항목", "yellow")

    click.echo()
    click.secho("진단 팁:", fg="cyan")
    click.echo("  - 살아있는데 로컬에 없음 → 다음 sync 에서 다운로드되어야 정상.")
    click.echo("  - 휴지통에 있음 + 로컬에 없음 + state 에 없음 → 정상 정리됨.")
    click.echo("  - 휴지통에 있는데 다른 PC 가 계속 다운로드 → 누군가 복원 중 (핑퐁 의심).")
    click.echo("  - explicitlyTrashed=true 면 사용자/SDK 가 직접 trash 한 것.")


@main.command("clean-empty-folders")
@click.argument("pair_name", required=False)
@click.option("--all", "all_pairs", is_flag=True, help="모든 sync 페어 대상")
@click.option("--apply", is_flag=True, help="실제 trash 이동/삭제 (기본은 dry-run)")
@click.option("--local", "local_mode", is_flag=True,
              help="Drive 대신 로컬 파일시스템의 빈 폴더 정리")
@click.option("--max-depth", default=10, show_default=True, help="검사 최대 깊이")
@click.pass_context
def clean_empty_folders(
    ctx, pair_name: Optional[str], all_pairs: bool, apply: bool,
    local_mode: bool, max_depth: int,
):
    """sync 페어 안의 '빈 폴더' 일괄 정리 (Drive 기본, --local 로 로컬).

    빈 폴더(파일/하위폴더 0개)는 gdrive-sync 가 추적하지 않아 양쪽 PC 간
    mkdir 핑퐁의 원인이 되고, 로컬에는 삭제된 폴더 껍데기로 남음. 이 명령으로
    한 번에 정리. (`.DS_Store`/`Thumbs.db`/`desktop.ini` 만 남은 폴더도 빈 것으로 간주.)

    안전망:
    - 기본 dry-run. 실제 이동/삭제는 --apply 명시 필요.
    - bottom-up 으로 처리해 자식 정리 후 부모가 빈 상태가 되면 같이 처리.
    - 페어 루트는 절대 제거하지 않음.

    예시:
      gdrive-sync clean-empty-folders Downloads               # Drive, 페어명 부분 일치, dry-run
      gdrive-sync clean-empty-folders --all                   # Drive, 모든 페어, dry-run
      gdrive-sync clean-empty-folders Downloads --apply       # Drive, 실제 정리
      gdrive-sync clean-empty-folders Downloads --local        # 로컬, dry-run
      gdrive-sync clean-empty-folders --all --local --apply    # 로컬, 모든 페어, 실제 정리
    """
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    if not pair_name and not all_pairs:
        click.secho("페어명(부분일치) 또는 --all 필요", fg="red", err=True)
        sys.exit(2)

    # 페어 필터
    if all_pairs:
        targets = list(cfg.sync_pairs)
    else:
        targets = [
            p for p in cfg.sync_pairs
            if pair_name.lower() in str(p.local_path).lower()
            or pair_name.lower() in p.remote_path.lower()
        ]
        if not targets:
            click.secho(f"매칭되는 페어 없음: {pair_name}", fg="red", err=True)
            sys.exit(2)

    # ── 로컬 모드: Drive 미접속, 파일시스템 빈 폴더 일괄 정리
    if local_mode:
        from gdrive_sync.sync_engine import sweep_empty_dirs

        mode_tag = "🟢 APPLY" if apply else "🟡 DRY-RUN"
        click.secho(f"\n{mode_tag} — 로컬 빈 폴더 정리", fg="cyan", bold=True)
        click.echo(f"대상 페어: {len(targets)}개")

        grand = 0
        for pair in targets:
            root = pair.local_path
            click.secho(f"\n▼ {root}", fg="cyan", bold=True)
            if not root.exists():
                click.secho("  경로 없음 — 건너뜀", fg="yellow")
                continue
            removed = sweep_empty_dirs(root, apply=apply, max_depth=max_depth)
            for rel in removed:
                click.secho(f"  ⊘ {rel}", fg="yellow")
            click.echo(f"  → {len(removed)}개")
            grand += len(removed)

        click.echo()
        click.secho(
            f"요약: 빈 폴더 {grand}개 {'정리 완료' if apply else '발견'}", fg="cyan",
        )
        if not apply and grand > 0:
            click.secho(
                "  ↳ 실제 정리하려면 --apply 추가: "
                "gdrive-sync clean-empty-folders ... --local --apply",
                fg="yellow",
            )
        return

    from gdrive_sync.drive_api import DriveClient, FOLDER_MIME
    try:
        drive = DriveClient(
            cfg.network,
            interactive_auth=False,
            performance=cfg.performance,
        )
    except Exception as e:
        click.secho(f"Drive 클라이언트 초기화 실패: {e}", fg="red", err=True)
        sys.exit(1)

    mode_tag = "🟢 APPLY" if apply else "🟡 DRY-RUN"
    click.secho(f"\n{mode_tag} — 빈 폴더 정리", fg="cyan", bold=True)
    click.echo(f"대상 페어: {len(targets)}개")

    grand_trashed = 0
    grand_kept = 0

    for pair in targets:
        click.secho(f"\n▼ {pair.remote_path}", fg="cyan", bold=True)
        try:
            rid = drive.resolve_folder_path(pair.remote_path, create_missing=False)
        except Exception as e:
            click.secho(f"  경로 해석 실패: {e}", fg="red")
            continue

        # 1) BFS 로 모든 폴더 수집 (depth, path, id 기록)
        all_folders: list[tuple[int, str, str]] = []  # (depth, path, id)
        queue = [(0, pair.remote_path.split("/")[-1], rid)]
        while queue:
            depth, path, fid = queue.pop(0)
            if depth > max_depth:
                continue
            all_folders.append((depth, path, fid))
            try:
                q = f"'{fid}' in parents and trashed = false and mimeType = '{FOLDER_MIME}'"
                resp = drive.service.files().list(
                    q=q, fields="files(id,name)", pageSize=1000,
                ).execute()
                for c in resp.get("files", []):
                    queue.append((depth + 1, f"{path}/{c['name']}", c["id"]))
            except Exception as e:
                click.secho(f"  ⚠ {path}: {e}", fg="yellow")

        click.echo(f"  검사 폴더 수: {len(all_folders)}개")

        # 2) bottom-up 처리: 깊은 폴더부터 — 자식이 정리되면 부모도 빈 상태가 될 수 있음
        all_folders.sort(key=lambda t: -t[0])
        trashed_ids: set[str] = set()
        # 루트는 빈 상태여도 절대 trash 안 함 (페어 루트는 보존)
        root_id = rid

        for depth, path, fid in all_folders:
            if fid == root_id:
                continue
            # 현재 자식 수 (방금 처리해 사라진 자식은 trashed_ids 로 제외)
            try:
                q = f"'{fid}' in parents and trashed = false"
                resp = drive.service.files().list(
                    q=q, fields="files(id)", pageSize=10,
                ).execute()
                children = [c for c in resp.get("files", []) if c["id"] not in trashed_ids]
            except Exception as e:
                click.secho(f"  ⚠ {path}: 자식 조회 실패: {e}", fg="yellow")
                continue

            if children:
                grand_kept += 1
                continue

            # 빈 폴더
            click.secho(f"  ⊘ {path}  id={fid}", fg="yellow")
            grand_trashed += 1
            if apply:
                try:
                    drive.delete_file(fid, permanent=False)
                    trashed_ids.add(fid)
                except Exception as e:
                    click.secho(f"    trash 실패: {e}", fg="red")

    click.echo()
    click.secho(
        f"요약: 빈 폴더 {grand_trashed}개 발견 / 정상 폴더 {grand_kept}개",
        fg="cyan",
    )
    if not apply and grand_trashed > 0:
        click.secho(
            f"  ↳ 실제 정리하려면 --apply 추가: gdrive-sync clean-empty-folders ... --apply",
            fg="yellow",
        )


@main.command()
@click.option("--edit", is_flag=True, help="기본 편집기로 설정 파일 열기")
@click.pass_context
def config(ctx, edit: bool):
    """현재 설정 출력/편집."""
    path: Path = ctx.obj["config_path"] or DEFAULT_CONFIG_PATH

    if edit:
        if not path.exists():
            click.secho(f"설정 파일이 없습니다: {path}", fg="red", err=True)
            sys.exit(2)
        editor = os.environ.get("EDITOR") or (
            "notepad" if sys.platform == "win32" else "nano"
        )
        subprocess.call([editor, str(path)])
        return

    cfg = _load_cfg_or_exit(path)
    click.secho(f"\n설정 파일: {cfg.config_path}", fg="cyan", bold=True)
    click.echo(f"  충돌 정책:  {cfg.conflict_policy}")
    click.echo(f"  삭제 정책:  {cfg.delete_policy}")
    click.echo(f"  로그 레벨:  {cfg.log_level}")
    click.echo(f"  프록시:     {'사용' if cfg.network.use_proxy else '직접연결'}")
    click.echo(f"  병렬 전송:  {cfg.performance.parallel_transfers}")
    click.echo(f"  대역폭:     {'활성' if cfg.bandwidth.enabled else '비활성'} "
               f"(up={cfg.bandwidth.upload_limit_mbps}, dn={cfg.bandwidth.download_limit_mbps} MB/s)")
    click.echo(f"  스케줄:     {'활성' if cfg.scheduler.enabled else '비활성'} "
               f"({len(cfg.scheduler.jobs)}개 작업)")
    click.echo("\n  동기화 쌍:")
    for p in cfg.sync_pairs:
        click.echo(f"    - {p.local_path}")
        click.echo(f"      ↔ {p.remote_path}")


@main.command("reset-state")
@click.confirmation_option(prompt="정말로 동기화 상태를 초기화하시겠습니까? (다음 실행 시 전체 비교)")
@click.pass_context
def reset_state(ctx):
    """동기화 상태 초기화."""
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    for pair in cfg.sync_pairs:
        if clear_state(pair.local_path):
            click.secho(f"✓ 상태 삭제: {pair.local_path}", fg="green")
        else:
            click.echo(f"  (없음)   {pair.local_path}")


# ──────────────────────────────────────────────────────────
# schedule 서브커맨드
# ──────────────────────────────────────────────────────────

@main.group()
def schedule():
    """OS 네이티브 스케줄러에 예약 작업 등록/관리."""
    pass


@schedule.command("list")
@click.pass_context
def schedule_list(ctx):
    """등록된 예약 작업 목록."""
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync.scheduler import get_scheduler
    s = get_scheduler()
    jobs = s.list_jobs()
    click.secho(f"\n[{s.__class__.__name__}] 등록된 작업", fg="cyan", bold=True)
    if not jobs:
        click.echo("  (없음)")
        return
    for j in jobs:
        click.echo(f"  • {j}")


@schedule.command("add")
@click.option("--name", required=True, help="작업 이름 (영숫자/언더스코어 권장)")
@click.option("--type", "sch_type",
              type=click.Choice(["daily", "weekly", "hourly", "interval", "cron"]),
              required=True,
              help="스케줄 타입")
@click.option("--time", "at_time", help="HH:MM (daily/weekly)")
@click.option("--weekdays", help="weekly용. 쉼표 구분 (예: mon,tue,wed,thu,fri)")
@click.option("--minute", type=int, help="hourly의 분 (0~59)")
@click.option("--interval", "interval_minutes", type=int,
              help="interval용 분 단위 간격")
@click.option("--cron", "cron_expr", help="cron 표현식 (type=cron일 때)")
@click.option("--options", default="", help="sync 명령에 전달할 옵션 (예: '--no-limit')")
@click.pass_context
def schedule_add(ctx, name, sch_type, at_time, weekdays, minute,
                 interval_minutes, cron_expr, options):
    """새 예약 작업 등록."""
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync.scheduler import default_python_executable, get_scheduler

    job = SchedulerJob(name=name, options=options)
    if sch_type == "cron":
        if not cron_expr:
            click.secho("--cron 표현식이 필요합니다", fg="red", err=True)
            sys.exit(2)
        job.cron = cron_expr
    elif sch_type == "daily":
        if not at_time:
            click.secho("--time HH:MM 이 필요합니다", fg="red", err=True)
            sys.exit(2)
        job.type = "daily"
        job.time = at_time
    elif sch_type == "weekly":
        if not at_time:
            click.secho("--time HH:MM 이 필요합니다", fg="red", err=True)
            sys.exit(2)
        job.type = "weekly"
        job.time = at_time
        job.weekdays = [d.strip() for d in (weekdays or "mon,tue,wed,thu,fri").split(",")]
    elif sch_type == "hourly":
        job.type = "hourly"
        job.minute = minute if minute is not None else 0
    elif sch_type == "interval":
        if not interval_minutes or interval_minutes <= 0:
            click.secho("--interval N 이 필요합니다", fg="red", err=True)
            sys.exit(2)
        job.type = "interval"
        job.interval_minutes = interval_minutes

    s = get_scheduler()
    python_exe = default_python_executable()
    try:
        label = s.register(job, python_exe)
    except Exception as e:
        click.secho(f"등록 실패: {e}", fg="red", err=True)
        sys.exit(1)

    click.secho(f"✓ 등록: {label}", fg="green")
    click.echo(f"  Python: {python_exe}")
    click.echo(f"  옵션:   sync {options}")


@schedule.command("remove")
@click.argument("name", required=False)
@click.option("--all", "all_jobs", is_flag=True, help="모든 예약 작업 제거")
@click.pass_context
def schedule_remove(ctx, name, all_jobs):
    """예약 작업 제거."""
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync.scheduler import get_scheduler
    s = get_scheduler()

    if all_jobs:
        removed = s.unregister_all()
        click.secho(f"✓ {removed}개 작업 제거됨", fg="green")
        return

    if not name:
        click.secho("작업 이름을 지정하거나 --all 옵션을 사용하세요", fg="red", err=True)
        sys.exit(2)

    if s.unregister(name):
        click.secho(f"✓ 제거: {name}", fg="green")
    else:
        click.secho(f"대상 없음: {name}", fg="yellow")


@schedule.command("install-from-config")
@click.pass_context
def schedule_install_from_config(ctx):
    """config.yaml의 scheduler.jobs 항목을 OS에 등록."""
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    if not cfg.scheduler.enabled:
        click.secho("scheduler.enabled = false 입니다. config 먼저 수정하세요.",
                    fg="yellow")
        return
    if not cfg.scheduler.jobs:
        click.echo("등록할 작업이 없습니다.")
        return

    from gdrive_sync.scheduler import default_python_executable, get_scheduler
    s = get_scheduler()
    python_exe = default_python_executable()

    ok = 0
    for job in cfg.scheduler.jobs:
        try:
            label = s.register(job, python_exe)
            click.secho(f"✓ {label}", fg="green")
            ok += 1
        except Exception as e:
            click.secho(f"✕ {job.name}: {e}", fg="red")
    click.echo(f"\n{ok}/{len(cfg.scheduler.jobs)} 개 등록 완료")


# ──────────────────────────────────────────────────────────
# doctor (진단/정리)
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--clean", is_flag=True, help="로그/휴지통 정리 (히스토리는 보존)")
@click.option("--clean-history", is_flag=True,
              help="동기화 히스토리(history.json) 삭제 — 통계 기록만 사라짐, 동기화 동작엔 영향 없음")
@click.pass_context
def doctor(ctx, clean: bool, clean_history: bool):
    """설치 상태 진단 + 불필요한 파일 정리."""
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync.cleanup import (
        diagnose, clean_logs, clean_trash_all, clean_history as _clean_history,
    )

    report = diagnose()

    # 카테고리별 아이콘
    icons = {
        "config": "⚙", "state": "📋", "trash": "🗑",
        "scheduler": "📅", "package": "📦", "log": "📝",
    }

    click.secho("\n=== gdrive-sync 설치 진단 ===\n", fg="cyan", bold=True)
    for item in report.items:
        icon = icons.get(item.category, "•")
        mark = click.style("✓", fg="green") if item.exists else click.style("—", fg="white")
        cleanable_mark = "  (정리 가능)" if item.cleanable and item.exists else ""
        detail = f"  {item.detail}" if item.detail else ""
        click.echo(f"  {mark}  {icon}  {item.path_or_name}{detail}{cleanable_mark}")

    # 정리 가능 요약
    cleanable = report.cleanable_items
    if cleanable:
        total = report.total_cleanable_bytes
        click.echo()
        click.secho(
            f"정리 가능: {len(cleanable)}개 항목 ({human_size(total)})",
            fg="yellow",
        )

    if not (clean or clean_history):
        if cleanable:
            click.echo("\n로그/휴지통을 정리하려면: gdrive-sync doctor --clean")
            click.echo("히스토리(통계 기록)도 지우려면: gdrive-sync doctor --clean-history")
        return

    click.echo()

    # --clean: 로그 + 휴지통 (히스토리 제외)
    if clean:
        if click.confirm("로그 파일을 삭제하시겠습니까?", default=True):
            cnt, size = clean_logs()
            click.secho(f"  ✓ 로그 {cnt}개 삭제 ({human_size(size)})", fg="green")

        trash_items = [i for i in cleanable if i.category == "trash"]
        if trash_items and click.confirm("휴지통을 비우시겠습니까?", default=True):
            cnt, size = clean_trash_all()
            click.secho(f"  ✓ 휴지통 {cnt}개 삭제 ({human_size(size)})", fg="green")

    # --clean-history: 명시적 옵트인만 히스토리 삭제
    if clean_history:
        if click.confirm(
            "동기화 히스토리를 삭제하시겠습니까? (GUI '동기화 히스토리' 통계가 비워짐)",
            default=False,
        ):
            cnt, size = _clean_history()
            click.secho(f"  ✓ 히스토리 {cnt}개 삭제 ({human_size(size)})", fg="green")


# ──────────────────────────────────────────────────────────
# uninstall (완전 제거)
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--keep-config", is_flag=True, help="설정 파일(config.yaml, token.json) 유지")
@click.option("--keep-package", is_flag=True, help="pip 패키지 유지 (파일만 정리)")
@click.option("--yes", "-y", is_flag=True, help="확인 없이 바로 실행")
@click.pass_context
def uninstall(ctx, keep_config: bool, keep_package: bool, yes: bool):
    """gdrive-sync의 모든 흔적을 깨끗하게 제거.

    동기화된 파일(구글드라이브/로컬)은 절대 삭제하지 않습니다.
    credentials.json은 보안상 자동 삭제하지 않습니다 (안내만 표시).
    """
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync.cleanup import diagnose, uninstall_all

    # 삭제 대상 미리보기
    report = diagnose()

    click.secho("\n=== gdrive-sync 완전 제거 ===\n", fg="red", bold=True)
    click.echo("다음 항목을 삭제합니다:\n")

    click.echo(f"  [1] 동기화 상태 파일 (.gdrive_sync_state.json)")
    click.echo(f"  [2] 로컬 휴지통 (.gdrive_sync_trash/)")
    click.echo(f"  [3] OS 예약 작업 (스케줄러)")
    if not keep_config:
        click.echo(f"  [4] 설정 디렉토리 ({DEFAULT_CONFIG_DIR})")
        click.echo(f"      (config.yaml, token.json, 로그, 히스토리 모두)")
    else:
        click.secho(f"  [4] 설정 디렉토리 — 유지 (--keep-config)", fg="yellow")
    if not keep_package:
        click.echo(f"  [5] Python 패키지 (pip uninstall gdrive-sync)")
    else:
        click.secho(f"  [5] Python 패키지 — 유지 (--keep-package)", fg="yellow")

    click.echo()
    click.secho("⚠  구글드라이브의 파일과 로컬 동기화 파일은 삭제하지 않습니다.", fg="yellow")
    click.secho("⚠  credentials.json은 수동 삭제가 필요합니다 (보안상).", fg="yellow")
    click.echo()

    if not yes:
        if not click.confirm("계속하시겠습니까?", default=False):
            click.echo("취소됨.")
            return

    # 실행
    def progress(idx, total, name):
        click.echo(f"  [{idx}/{total}] {name}...")

    steps = uninstall_all(
        remove_config=not keep_config,
        remove_states=True,
        remove_trash=True,
        remove_scheduler=True,
        remove_package=not keep_package,
        progress_cb=progress,
    )

    # 결과 출력
    click.echo()
    for step in steps:
        if step.success:
            click.secho(f"  ✓ {step.name}: {step.detail}", fg="green")
        elif step.error:
            click.secho(f"  ✕ {step.name}: {step.error}", fg="red")

    # Windows 우클릭 메뉴 정리 (등록돼 있었다면)
    from gdrive_sync import context_menu as cm
    if cm.is_supported():
        try:
            removed = cm.remove()
            if removed:
                click.secho(f"  ✓ 우클릭 메뉴: {len(removed)}곳 제거", fg="green")
        except OSError as e:
            click.secho(f"  ✕ 우클릭 메뉴: {e}", fg="red")

    # 수동 삭제 안내
    click.echo()
    click.secho("완료. 다음 항목만 수동 삭제가 필요합니다:", fg="cyan")
    click.echo(f"  - credentials.json (OAuth 시크릿 — 프로젝트 폴더에 있음)")
    click.echo(f"  - 프로젝트 소스 코드 폴더 (필요 없으면 직접 삭제)")
    if keep_config:
        click.echo(f"  - {DEFAULT_CONFIG_DIR} (--keep-config으로 유지됨)")


# ──────────────────────────────────────────────────────────
# normalize (NFD → NFC 일괄 정규화)
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--dry-run", is_flag=True, help="실제 변경 없이 어떤 파일이 바뀔지 미리보기")
@click.option("--remote", "remote_mode", is_flag=True,
              help="로컬 대신 Drive 쪽 파일/폴더 이름을 서버측 rename 으로 정규화")
@click.option("--path", "extra_paths", multiple=True, type=click.Path(),
              help="설정의 sync_pairs 외에 추가로 정규화할 경로 (반복 지정 가능)")
@click.option("--only-path", "only_paths", multiple=True, type=click.Path(),
              help="이 경로만 정규화 (sync_pairs 무시)")
@click.option("--yes", "-y", is_flag=True, help="확인 없이 바로 실행")
@click.pass_context
def normalize(ctx, dry_run: bool, remote_mode: bool, extra_paths: tuple,
              only_paths: tuple, yes: bool):
    """파일/폴더 이름의 한글 자모 분절(NFD)을 완성형(NFC)으로 정규화.

    macOS에서 동기화된 파일이 Windows에서 'ㅈㅜㅅ ㅣ ㅂㅈ ㅏ...'처럼
    분절돼 보이는 경우 사용. 기본적으로 설정의 모든 sync_pairs.local_path를
    대상으로 합니다.

    --remote: 맥에서 브라우저로 Drive에 직접 올려 NFD 이름으로 저장된
    파일/폴더를 Drive 서버측 rename(재전송 없음, modifiedTime 보존)으로 정리.
    """
    from gdrive_sync.normalize import normalize_path

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    _setup_logging(cfg, verbose=ctx.obj["verbose"])

    if remote_mode:
        _normalize_remote(cfg, dry_run=dry_run, yes=yes)
        return

    if only_paths:
        targets = [Path(p).expanduser().resolve() for p in only_paths]
    else:
        targets = [pair.local_path for pair in cfg.sync_pairs]
        targets += [Path(p).expanduser().resolve() for p in extra_paths]

    if not targets:
        click.secho("정규화할 폴더가 없습니다. config의 sync_pairs를 확인하세요.",
                    fg="yellow")
        return

    click.secho(f"\n=== 한글 파일명 정규화 (NFD → NFC) ===", fg="cyan", bold=True)
    click.echo(f"  모드:   {'미리보기 (dry-run)' if dry_run else '실제 변경'}")
    click.echo(f"  대상:   {len(targets)}개 폴더")
    for t in targets:
        click.echo(f"    - {t}")

    if not dry_run and not yes:
        click.echo()
        if not click.confirm("계속하시겠습니까?", default=True):
            click.echo("취소됨.")
            return

    total_scan = total_fix = total_renamed = total_conflict = total_err = 0
    for t in targets:
        click.secho(f"\n→ {t}", fg="blue")
        rep = normalize_path(t, dry_run=dry_run)
        total_scan += rep.scanned
        total_fix += rep.needs_fix
        total_renamed += rep.renamed
        total_conflict += rep.skipped_conflict
        total_err += rep.errors
        click.echo(f"  검사: {rep.scanned}  /  정규화 필요: {rep.needs_fix}  "
                   f"/  변경: {rep.renamed}  /  충돌: {rep.skipped_conflict}  "
                   f"/  오류: {rep.errors}")
        # 변경 예시 출력 (최대 5건)
        for src, dst in rep.changes[:5]:
            click.echo(f"    · {src.name}  →  {dst.name}")
        if len(rep.changes) > 5:
            click.echo(f"    · ...외 {len(rep.changes)-5}건")
        for src in rep.conflicts[:3]:
            click.secho(f"    ! 충돌: {src} (대상이 이미 존재)", fg="yellow")

    click.echo()
    click.secho("=== 합계 ===", fg="cyan", bold=True)
    click.echo(f"  검사 {total_scan}  / 정규화 필요 {total_fix}  "
               f"/ 변경 {total_renamed}  / 충돌 {total_conflict}  / 오류 {total_err}")
    if dry_run and total_fix:
        click.secho("  --dry-run 모드 — 실제로 변경하려면 옵션 없이 다시 실행하세요.",
                    fg="yellow")


def _normalize_remote(cfg, dry_run: bool, yes: bool) -> None:
    """Drive 쪽 NFD 이름 일괄 정규화 (normalize --remote 구현부)."""
    from gdrive_sync.drive_api import DriveClient
    from gdrive_sync.normalize import normalize_remote_entries

    try:
        drive = DriveClient(
            cfg.network, interactive_auth=False, performance=cfg.performance,
        )
    except Exception as e:
        click.secho(f"Drive 클라이언트 초기화 실패: {e}", fg="red", err=True)
        sys.exit(1)

    click.secho("\n=== Drive 한글 파일명 정규화 (NFD → NFC, 서버측 rename) ===",
                fg="cyan", bold=True)
    click.echo(f"  모드:   {'미리보기 (dry-run)' if dry_run else '실제 변경'}")
    click.echo(f"  대상:   sync 페어 {len(cfg.sync_pairs)}개의 Drive 폴더")

    if not dry_run and not yes:
        click.echo()
        if not click.confirm("계속하시겠습니까?", default=True):
            click.echo("취소됨.")
            return

    total_fix = total_renamed = total_conflict = total_err = 0
    for pair in cfg.sync_pairs:
        click.secho(f"\n→ {pair.remote_path}", fg="blue")
        try:
            rid = drive.resolve_folder_path(pair.remote_path, create_missing=False)
        except FileNotFoundError:
            click.secho("  Drive 폴더 없음 — 건너뜀", fg="yellow")
            continue
        except Exception as e:
            click.secho(f"  경로 해석 실패: {e}", fg="red")
            continue

        entries = [(rel, df) for rel, df in drive.list_tree(rid)]
        rep = normalize_remote_entries(drive, entries, dry_run=dry_run)
        total_fix += rep.needs_fix
        total_renamed += rep.renamed
        total_conflict += rep.skipped_conflict
        total_err += rep.errors
        click.echo(f"  검사: {rep.scanned}  /  정규화 필요: {rep.needs_fix}  "
                   f"/  변경: {rep.renamed}  /  충돌: {rep.skipped_conflict}  "
                   f"/  오류: {rep.errors}")
        for old_rel, new_rel in rep.changes[:5]:
            click.echo(f"    · {old_rel}  →  {new_rel.rsplit('/', 1)[-1]}")
        if len(rep.changes) > 5:
            click.echo(f"    · ...외 {len(rep.changes)-5}건")
        for rel in rep.conflicts[:3]:
            click.secho(f"    ! 충돌: {rel} (NFC 동명 항목 존재)", fg="yellow")

    click.echo()
    click.secho("=== 합계 ===", fg="cyan", bold=True)
    click.echo(f"  정규화 필요 {total_fix}  / 변경 {total_renamed}  "
               f"/ 충돌 {total_conflict}  / 오류 {total_err}")
    if dry_run and total_fix:
        click.secho("  --dry-run 모드 — 실제로 변경하려면 --dry-run 빼고 다시 실행하세요.",
                    fg="yellow")
    if not dry_run and total_renamed:
        click.secho(
            "  ↳ 로컬에 이미 NFD 이름으로 내려온 파일은 'gdrive-sync normalize' "
            "(로컬) 또는 다음 sync 가 자동 정리합니다.", fg="cyan",
        )


# ──────────────────────────────────────────────────────────
# gui (v2)
# ──────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def gui(ctx):
    """간단한 GUI 실행 (Tkinter 기반)."""
    try:
        from gdrive_sync.gui import run_gui
    except ImportError as e:
        click.secho(f"GUI 모듈 로드 실패: {e}", fg="red", err=True)
        click.secho(
            "Tkinter가 설치돼 있는지 확인하세요. (대부분 Python 표준 포함)",
            fg="yellow",
        )
        sys.exit(1)
    try:
        cfg = _load_cfg_or_exit()
        _setup_logging(cfg, verbose=ctx.obj["verbose"])
    except SystemExit:
        _setup_logging(verbose=ctx.obj["verbose"])
    run_gui()


# ──────────────────────────────────────────────────────────
# context-menu (Windows 우클릭 메뉴)
# ──────────────────────────────────────────────────────────

@main.command("context-menu")
@click.option("--remove", "do_remove", is_flag=True, help="우클릭 메뉴 항목 제거")
@click.option("--status", "show_status", is_flag=True, help="등록 상태만 확인")
@click.pass_context
def context_menu_cmd(ctx, do_remove: bool, show_status: bool):
    """Windows 우클릭 메뉴에 GUI 실행 항목 등록/제거.

    등록하면 바탕화면·탐색기 폴더 빈 공간에서 우클릭 →
    'gdrive-sync GUI 실행'으로 콘솔 창 없이 GUI를 띄울 수 있습니다.
    HKCU(현재 사용자)에만 기록하므로 관리자 권한이 필요 없습니다.
    """
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync import context_menu as cm

    if not cm.is_supported():
        click.secho("이 명령은 Windows에서만 사용할 수 있습니다.", fg="red", err=True)
        sys.exit(1)

    if show_status:
        for path, registered in cm.status().items():
            mark = "O" if registered else "X"
            state = "등록됨" if registered else "없음"
            click.echo(f"  [{mark}] {path} — {state}")
        return

    if do_remove:
        removed = cm.remove()
        if removed:
            click.secho(f"우클릭 메뉴 제거 완료 ({len(removed)}곳)", fg="green")
        else:
            click.echo("등록된 항목이 없습니다.")
        return

    written = cm.install()
    click.secho(f"우클릭 메뉴 등록 완료: '{cm.MENU_TEXT}'", fg="green")
    for path in written:
        click.echo(f"  - {path}")
    click.echo("바탕화면 또는 폴더 빈 공간에서 우클릭하면 나타납니다.")
    click.echo(f"실행 명령: {cm.launch_command()}")


# ──────────────────────────────────────────────────────────
# shortcut (v2.4.1 — 바탕화면 바로가기)
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--remove", "do_remove", is_flag=True, help="바로가기 삭제")
@click.pass_context
def shortcut(ctx, do_remove: bool):
    """바탕화면에 'gdrive-sync' GUI 바로가기 생성 (Windows).

    모든 Windows 버전에서 동일하게 동작합니다. Windows 11 은 우클릭 메뉴
    항목이 '추가 옵션 표시' 안에만 나오므로, 더블클릭 실행은 이 바로가기가
    가장 간단합니다.
    """
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync import context_menu as cm

    if not cm.is_supported():
        click.secho("이 명령은 Windows에서만 사용할 수 있습니다.", fg="red", err=True)
        sys.exit(1)

    try:
        if do_remove:
            removed = cm.remove_desktop_shortcut()
            if removed:
                click.secho(f"바로가기 삭제: {removed}", fg="green")
            else:
                click.echo("바탕화면에 바로가기가 없습니다.")
        else:
            path = cm.create_desktop_shortcut()
            click.secho(f"바탕화면 바로가기 생성 완료: {path}", fg="green")
            click.echo("더블클릭하면 콘솔 창 없이 GUI가 실행됩니다.")
    except Exception as e:
        click.secho(f"바로가기 처리 실패: {e}", fg="red", err=True)
        sys.exit(1)


# ──────────────────────────────────────────────────────────
# update (v2.4 — 배포/업데이트)
# ──────────────────────────────────────────────────────────

@main.command()
@click.option("--check-only", is_flag=True, help="새 버전 확인만 하고 설치는 안 함")
@click.option("--yes", "-y", is_flag=True, help="확인 질문 없이 바로 설치")
@click.pass_context
def update(ctx, check_only: bool, yes: bool):
    """GitHub 저장소에서 새 버전을 확인하고 업데이트.

    개발자가 새 버전 태그(v0.2.0 형식)를 push 하면 이 명령으로
    현재 파이썬 환경에 최신 버전을 설치합니다. (pip 사용)
    """
    _setup_logging(verbose=ctx.obj["verbose"])
    from gdrive_sync.update_check import check_for_update, run_pip_upgrade

    click.echo("새 버전 확인 중...")
    info = check_for_update(force=True, timeout=8.0)

    if info is None:
        click.secho(
            "버전 정보를 가져오지 못했습니다. 네트워크 또는 저장소 접근 권한을 확인하세요.\n"
            "(비공개 저장소는 git 로그인 자격증명이 필요합니다)",
            fg="red", err=True,
        )
        sys.exit(1)

    if not info.available:
        click.secho(f"이미 최신 버전입니다 (v{info.current}).", fg="green")
        return

    click.secho(f"새 버전 발견: v{info.current} → v{info.latest}", fg="cyan", bold=True)
    if check_only:
        click.echo(f"설치하려면: gdrive-sync update  (또는 {info.upgrade_command()})")
        return

    if not yes and not click.confirm("지금 업데이트할까요?", default=True):
        return

    rc, _ = run_pip_upgrade()
    if rc == 0:
        click.secho(f"v{info.latest} 업데이트 완료. 실행 중인 GUI가 있다면 재시작하세요.", fg="green")
    else:
        click.secho(f"pip 업그레이드 실패 (종료코드 {rc}). 위 로그를 확인하세요.", fg="red", err=True)
        sys.exit(rc)


if __name__ == "__main__":
    main()
