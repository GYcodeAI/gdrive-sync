# gdrive-sync 프로젝트 컨텍스트

## 프로젝트 개요
Python 크로스플랫폼(Windows/macOS/Linux) 구글드라이브 양방향 동기화 CLI 프로그램.
3-way diff 기반 동기화 엔진으로 안전한 충돌 감지 및 해결 제공.

## 기술 스택
- Python 3.9+ (3.14 검증)
- click (CLI) / google-api-python-client v3 (Drive API) / OAuth 2.0
- httplib2 + PySocks (선택적 프록시) / tqdm / pyyaml
- concurrent.futures.ThreadPoolExecutor (병렬 전송)
- plistlib (macOS launchd plist 생성)

## 아키텍처 요약
```
cli.py  ─▶  sync_engine.py  ─┬─▶ transfer_pool.py (병렬 워커)
                             │      └─▶ drive_api.py (스레드별 DriveClient)
                             ├─▶ drive_api.py   (Google Drive)
                             ├─▶ local_scanner.py (로컬 FS)
                             ├─▶ state.py        (.gdrive_sync_state.json)
                             ├─▶ conflict.py     (정책 적용)
                             └─▶ bandwidth.py    (토큰 버킷 + 스케줄)
scheduler.py ─▶ Windows schtasks / macOS launchd / Linux crontab
auth.py  ─▶ credentials.json + token.json
network.py ─▶ httplib2 Http 객체 (프록시 주입)
config.py  ─▶ config.yaml + device_overrides + performance/bandwidth/scheduler
```

## 동기화 로직 (3-way)
이전 state ↔ 현재 로컬 ↔ 현재 리모트 비교.
- 9가지 상태 조합 테이블로 액션 결정 (`sync_engine.py`의 `_decide_action` 참조)
- 변경 감지: size + mtime + md5
- 충돌 정책: newer_wins / local_wins / remote_wins / keep_both
- 삭제 정책: trash / permanent / skip

## 현재 상태 (v2.3)
- v1: 초기 구현 (35 tests)
- v2: 병렬 전송 + 대역폭 제한 + 예약 작업 (87 tests)
- v2.1: httplib2 308 버그 자동 회피 패치
- v2.2: GUI 5대 기능 (토스트 알림, 개별 폴더 동기화, 대역폭 편집기, 스케줄러 GUI, 히스토리 패널)
- v2.3: 이동/이름변경 감지 (delete+new → move 변환, 재전송 생략) + Drive 빈 폴더 자동 정리 (198 tests)
- v2.4: 배포/자동 업데이트 — pip+GitHub 태그 기반 (227 tests)

## 주요 구현 사항
- **병렬 전송** (`transfer_pool.py`): ThreadPoolExecutor, 스레드별 DriveClient, 공유 path_cache (Lock)
- **Simple upload 분기** (`drive_api.py`): 5MB 미만 single POST / 5~100MB=8MB 청크 / 100MB+=32MB 청크
- **대역폭 제한** (`bandwidth.py`): 토큰 버킷 + 시간대별 스케줄 + 자정 넘김 지원
- **예약 작업** (`scheduler.py`): `sys.executable` 자동 감지
- **폴더 경쟁 방지**: 병렬 업로드 전 상위 폴더 사전 생성 + `_path_cache` Lock
- **이동/이름변경 감지** (`sync_engine.py`의 `_detect_renames`): 삭제+신규 쌍을 size+md5 로 매칭 →
  MOVE_REMOTE(Drive 서버측 이동, modifiedTime 보존) / MOVE_LOCAL(로컬 이동). 0바이트 파일은 제외
- **Drive 빈 폴더 자동 정리** (`_prune_empty_remote_dirs`): 파일 삭제/이동으로 비게 된 폴더를
  bottom-up trash — 폴더 rename 후 옛 이름 폴더가 Drive 에 남는 문제 해결. delete_policy=skip 이면 안 함
- **Drive 쪽 NFD 파일명 정규화** (`normalize.py`의 `normalize_remote_entries` + sync 스캔 3.5단계):
  맥에서 브라우저로 직접 업로드된 NFD(자모분리) 이름을 서버측 rename 으로 NFC 수정 (modifiedTime 보존).
  `auto_normalize_filenames` 옵션이 로컬+Drive 양쪽 커버. 일괄 정리는 `normalize --remote`
- **배포/자동 업데이트** (`update_check.py`): GitHub 태그(v1.2.3 형식)로 새 버전 감지 —
  API(public)→`git ls-remote`(private) 폴백, 24h 스로틀. CLI `gdrive-sync update`, GUI 도움말 메뉴,
  sync 후 힌트. 릴리스 = `__init__.py` 버전 올리고 tag push (버전은 이 한 곳, pyproject 는 dynamic 참조).
  받는 사람 설치 안내는 `docs/INSTALL.md` (OAuth 직접 발급 포함), 더블클릭 업데이트는 `scripts/update.bat`

## 주의사항
- `credentials.json`, `token.json`, `config.yaml`은 **절대 git commit 금지** (`.gitignore` 등록됨)
- `config.yaml`의 `local_path`는 `~/`로 시작하면 Windows/Mac 공용 가능
- 기기별 다른 경로는 `device_overrides.<hostname>`로 덮어씀
- Google Workspace 네이티브 파일(docs/sheets 등)은 동기화 제외 (로그로만 알림)
- 삭제는 기본 `trash` 모드 — 복구 가능

## 다음 할 일 (TODO)
- 실제 Google Cloud Console OAuth 발급 후 `gdrive-sync auth` 검증
- Mac 환경에서 `gdrive-sync schedule add` launchd 등록 검증
- 대용량 파일(>100MB) resumable upload 실전 테스트
- Google Workspace 문서 선택적 export 옵션 추가 검토
- 이동/이름변경 감지 실전 검증 (v2.3 구현 완료 — dry-run 에서 39건 감지 확인, 실제 sync 1회 관찰 필요)
- 첫 릴리스 태그(v2.4.0) push 후 알림→업데이트 사이클 실전 검증
- B안: PyInstaller exe 배포 (비개발자용 — HANDOFF 2026-08-11 세션의 "향후 과제" 참조)

---

## 클로드 작업 운용 가이드라인

### 서브에이전트 위임 규칙
- 동기화 로그 분석은 서브에이전트로 위임 후 요약만 수령
- 대용량 응답(API 응답, 디렉토리 트리)은 서브에이전트 컨텍스트에 머물게 함
- 예: "logs/sync_*.log 분석해서 충돌 발생 파일/시각/패턴 3개만 요약 보고"

### /clear 시점
- GUI 작업 ↔ 동기화 엔진 작업 전환 시
- 디버깅 세션 종료 후 새 기능 시작 시

### Compact 시 우선순위
- 보존: 동기화 알고리즘 결정, 충돌 해결 패턴, 진행 중 버그 수정 컨텍스트
- 드롭: GUI 스타일링 논의, 로그 덤프, 일회성 시도/실패 출력