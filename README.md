# gdrive-sync

크로스플랫폼(Windows/macOS/Linux) 구글드라이브 양방향 동기화 CLI.
3-way diff 엔진으로 안전한 충돌 감지 및 해결을 제공합니다.

## 주요 기능

- **양방향 동기화**: 이전 상태(state) ↔ 현재 로컬 ↔ 현재 리모트 3-way diff
- **병렬 전송 (v2)**: 최대 10개 파일 동시 전송 — FreeFileSync 기법 참고
- **대역폭 제한 (v2)**: 토큰 버킷 알고리즘 + 시간대별 스케줄 (업무시간/점심/야간)
- **예약 작업 (v2)**: Windows 작업스케줄러 / macOS launchd / Linux crontab 크로스플랫폼
- **Simple/Resumable 자동 분기 (v2)**: 5MB 미만은 단일 POST, 그 이상은 resumable + 청크 자동 조절
- **크로스플랫폼**: `~` 경로로 Windows/Mac/Linux 공용 설정 파일
- **기기별 덮어쓰기**: hostname 기반 `device_overrides`
- **충돌 해결**: `newer_wins` / `local_wins` / `remote_wins` / `keep_both`
- **안전한 삭제**: 휴지통 기본, 복구 가능
- **선택적 프록시**: HTTP / SOCKS5 (기업 방화벽 대응)
- **한글 파일명 완벽 지원**

## 설치

요구사항: **Python 3.9 이상** (3.14에서 검증). Python 표준 Tkinter만 사용하므로 추가 GUI 라이브러리 설치 불필요.

---

### 🪟 Windows 설치

#### 1) Python 설치 확인
PowerShell 또는 명령 프롬프트에서:
```powershell
py -3.14 --version
```
`Python 3.14.x` 형식이 안 나오면 https://www.python.org/downloads/windows/ 에서 설치.
**설치 시 반드시 "Add python.exe to PATH" 체크**.

#### 2) Git 설치 확인
```powershell
git --version
```
없으면 https://git-scm.com/download/win 에서 설치.

#### 3) 프로젝트 clone + 패키지 설치
```powershell
mkdir C:\Users\%USERNAME%\claude
cd C:\Users\%USERNAME%\claude
git clone https://github.com/GYcodeAI/gdrive-sync.git
cd gdrive-sync
py -3.14 -m pip install -e .
```

#### 4) `credentials.json` 배치
Google Cloud Console에서 발급받은 `credentials.json`을 프로젝트 루트에 복사:
```powershell
copy C:\Users\%USERNAME%\Downloads\credentials.json C:\Users\%USERNAME%\claude\gdrive-sync\
```

#### 5) 실행
```powershell
# CLI
py -3.14 -m gdrive_sync init
py -3.14 -m gdrive_sync auth
py -3.14 -m gdrive_sync sync --dry-run
py -3.14 -m gdrive_sync sync

# GUI (콘솔 없이)
py -3.14 -m gdrive_sync gui
```

또는 탐색기에서 **`launch-gui.vbs`** 더블클릭 (콘솔 창 없이 GUI만 뜸).
**`launch-gui.bat`**는 `pythonw.exe`를 사용한 배치 실행, **`launch-gui-debug.bat`**은 디버그용(오류 메시지 보임).

#### 우클릭 메뉴 등록 (선택)
```powershell
py -3.14 -m gdrive_sync context-menu            # 등록
py -3.14 -m gdrive_sync context-menu --status   # 상태 확인
py -3.14 -m gdrive_sync context-menu --remove   # 제거
```
등록하면 **바탕화면·탐색기 폴더 빈 공간 우클릭 → "gdrive-sync GUI 실행"**으로
콘솔 창 없이 GUI를 띄울 수 있습니다. HKCU에만 기록되므로 관리자 권한 불필요.
Python/가상환경 경로가 바뀌면 다시 등록하세요. (`gdrive-sync uninstall` 시 자동 제거)

---

### 🍎 macOS 설치

#### 1) Python 설치 확인
Terminal.app에서:
```bash
python3 --version
```
`Python 3.9` 이상이어야 함. 없거나 낮으면 **Homebrew**로 설치:
```bash
# Homebrew 설치 (이미 있으면 생략)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Python 3.14 설치
brew install python@3.14
```

#### 2) 프로젝트 clone + 패키지 설치
```bash
mkdir -p ~/claude
cd ~/claude
git clone https://github.com/GYcodeAI/gdrive-sync.git
cd gdrive-sync
pip3 install -e .
```

> 💡 **apple silicon(M1/M2/M3)에서 `pip3` 경고가 뜨면**: `python3 -m pip install --user -e .` 사용. 또는 가상환경:
> ```bash
> python3 -m venv .venv
> source .venv/bin/activate
> pip install -e .
> ```

#### 3) `credentials.json` 배치
Windows PC에서 발급받은 `credentials.json`을 맥북으로 복사 (USB/AirDrop/카톡 나와의 채팅 등):
```bash
# Downloads에 있다고 가정
cp ~/Downloads/credentials.json ~/claude/gdrive-sync/
```

#### 4) 실행
```bash
cd ~/claude/gdrive-sync

# CLI
python3 -m gdrive_sync init
python3 -m gdrive_sync auth
python3 -m gdrive_sync sync --dry-run
python3 -m gdrive_sync sync

# GUI
python3 -m gdrive_sync gui
```

또는 Finder에서 **`launch-gui.command`** 더블클릭. **최초 1회**만 실행 권한 부여:
```bash
chmod +x ~/claude/gdrive-sync/launch-gui.command
```

**Gatekeeper 경고** ("확인되지 않은 개발자") 뜨면:
- 파일 **우클릭 → 열기** → 경고 창에서 **"열기"** 클릭 (다음부턴 바로 실행됨)
- 또는 시스템 설정 → 개인정보 보호 및 보안 → "어쨌든 열기"

---

### 🐧 Linux 설치 (Ubuntu/Debian 기준)

#### 1) Python + Git + Tkinter 설치
```bash
sudo apt update
sudo apt install python3 python3-pip python3-venv python3-tk git -y
python3 --version
```
> `python3-tk`는 Tkinter 패키지 (GUI용). Ubuntu 기본 Python엔 없으므로 따로 설치 필요.

#### 2) 프로젝트 clone + 패키지 설치
```bash
mkdir -p ~/claude
cd ~/claude
git clone https://github.com/GYcodeAI/gdrive-sync.git
cd gdrive-sync
pip3 install --user -e .
# 또는 가상환경
# python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

#### 3) `credentials.json` 배치
```bash
cp ~/Downloads/credentials.json ~/claude/gdrive-sync/
```

#### 4) (선택) 데스크톱 알림 지원
```bash
sudo apt install libnotify-bin -y
```

#### 5) 실행
```bash
cd ~/claude/gdrive-sync
python3 -m gdrive_sync init
python3 -m gdrive_sync auth
python3 -m gdrive_sync sync --dry-run
python3 -m gdrive_sync sync

# GUI
python3 -m gdrive_sync gui
```

---

### 멀티 디바이스 설정 동기화 팁

`config.yaml`의 `remote_path`(구글드라이브 경로)만 기기 간 **동일**하게 맞추면 같은 폴더를 보게 됩니다. 로컬 경로는 달라도 무관:

| 기기 | local_path | remote_path |
|------|-----------|-------------|
| Windows (회사) | `D:\업무폴더` | `회사문서/업무폴더` |
| macOS (집) | `~/업무폴더` | `회사문서/업무폴더` |

기기별로 `~/.gdrive_sync/config.yaml`을 각자 `init`으로 생성해서 사용하세요.

## 사전 준비: Google Cloud Console OAuth 설정

OAuth 2.0은 "구글 아이디/비밀번호를 프로그램에 직접 입력"하는 방식이 아닙니다.
브라우저에서 구글 로그인 화면이 뜨고, 한 번 "허용"만 누르면 끝입니다.
단, 구글이 "어떤 프로그램이 접근을 요청하는지" 알도록 프로젝트를 한 번 등록해야 합니다.

1. https://console.cloud.google.com/ 접속
2. 상단 프로젝트 선택 → **새 프로젝트** → 이름: `gdrive-sync`
3. **API 및 서비스** → **라이브러리** → `Google Drive API` 검색 → **사용** 클릭
4. **OAuth 동의 화면** → **외부** → 앱 이름 `gdrive-sync`, 지원 이메일 입력 → 저장
5. **테스트 사용자** → 본인 Gmail 주소 추가
6. **사용자 인증 정보** → **+ 사용자 인증 정보 만들기** → **OAuth 클라이언트 ID**
7. 애플리케이션 유형: **데스크톱 앱** → 이름 `gdrive-sync` → 만들기
8. **JSON 다운로드** → `credentials.json`으로 이름 변경 → 프로젝트 루트에 배치

> 최초 1회만 하면 됩니다. 이후 refresh token으로 자동 갱신.

## 빠른 시작

```bash
# 1) 설정 초기화 (대화형)
gdrive-sync init

# 2) 구글 인증 (브라우저 자동 열림 → 허용)
gdrive-sync auth

# 3) 네트워크 연결 확인
gdrive-sync test-connection

# 4) 먼저 dry-run으로 변경사항 확인!
gdrive-sync sync --dry-run

# 5) 실제 동기화
gdrive-sync sync
```

## 명령어

| 명령어 | 설명 |
|--------|------|
| `gdrive-sync init` | 설정 파일 초기화 |
| `gdrive-sync auth` | 구글 OAuth 인증 |
| `gdrive-sync auth --revoke` | 토큰 삭제 후 재인증 |
| `gdrive-sync test-connection` | 네트워크 연결 진단 |
| `gdrive-sync sync` | 동기화 실행 |
| `gdrive-sync sync --dry-run` | 미리보기 (실제 전송 없음) |
| `gdrive-sync sync --force-upload` | 로컬 → 리모트 강제 업로드 |
| `gdrive-sync sync --force-download` | 리모트 → 로컬 강제 다운로드 |
| `gdrive-sync status` | 마지막 동기화 상태 |
| `gdrive-sync config` | 현재 설정 출력 |
| `gdrive-sync config --edit` | 편집기로 설정 열기 |
| `gdrive-sync reset-state` | 상태 초기화 (다음 실행 시 전체 비교) |
| `gdrive-sync sync --parallel N` | 동시 전송 수 지정 (1~10, v2) |
| `gdrive-sync sync --upload-limit 2` | 업로드 2 MB/s 제한 (v2) |
| `gdrive-sync sync --download-limit 5` | 다운로드 5 MB/s 제한 (v2) |
| `gdrive-sync sync --no-limit` | 대역폭 제한 모두 해제 (v2) |
| `gdrive-sync schedule list` | 예약 작업 목록 (v2) |
| `gdrive-sync schedule add ...` | 예약 작업 등록 (v2) |
| `gdrive-sync schedule remove <name>` | 예약 작업 제거 (v2) |
| `gdrive-sync schedule install-from-config` | config.yaml에서 일괄 등록 (v2) |

## 설정 파일 (`config.yaml`)

기본 위치: `~/.gdrive_sync/config.yaml`.
전체 스펙은 `config.example.yaml` 참고.

### 크로스플랫폼 경로 팁

```yaml
sync_pairs:
  - local_path: "~/GDriveSync"     # Windows: C:\Users\ky0917\GDriveSync
                                   # Mac:     /Users/ky0917/GDriveSync
    remote_path: "동기화테스트"
```

`~`로 시작하면 **같은 설정 파일을 Windows와 Mac에서 그대로 공유** 가능합니다.

### 기기별 다른 경로가 필요할 때

```yaml
device_overrides:
  "DESKTOP-OFFICE":                # Windows 회사 PC의 hostname
    sync_pairs:
      - local_path: "D:/Work/Projects"
        remote_path: "업무/프로젝트"
  "MacBook-Home":
    sync_pairs:
      - local_path: "~/Projects"
        remote_path: "업무/프로젝트"
```

hostname 확인:
- Windows: 명령프롬프트에서 `hostname`
- Mac/Linux: 터미널에서 `hostname`

## v2 성능 기능 사용법

### 병렬 전송
```bash
# 동시 5개 (기본)
gdrive-sync sync

# 동시 10개 (고속 회선)
gdrive-sync sync --parallel 10

# 동시 2개 (회사망 보호)
gdrive-sync sync --parallel 2
```

또는 `~/.gdrive_sync/config.yaml`에서:
```yaml
performance:
  parallel_transfers: 5
```

### 대역폭 제한 (회사망 과부하 방지)

**CLI로 즉석 제한**:
```bash
gdrive-sync sync --upload-limit 2 --download-limit 5    # 업로드 2 MB/s, 다운로드 5 MB/s
gdrive-sync sync --no-limit                              # 제한 완전 해제
```

**시간대별 자동 전환** (`config.yaml`):
```yaml
bandwidth:
  enabled: true
  upload_limit_mbps: 0       # 기본: 무제한
  download_limit_mbps: 0
  schedule:
    - name: "업무시간"
      time_start: "09:00"
      time_end: "18:00"
      weekdays: ["mon","tue","wed","thu","fri"]
      upload_limit_mbps: 2.0
      download_limit_mbps: 3.0
    - name: "점심"
      time_start: "12:00"
      time_end: "13:00"
      weekdays: ["mon","tue","wed","thu","fri"]
      upload_limit_mbps: 0    # 점심엔 해제
      download_limit_mbps: 0
```

규칙은 위부터 검사, 처음 일치하는 것이 적용됩니다. 자정을 넘는 시간대(예: 22:00~07:00)도 자동 처리됩니다.

### 예약 작업 (크로스플랫폼)

OS 네이티브 스케줄러에 등록합니다 (프로그램이 상주할 필요 없음).

| OS | 백엔드 | 저장 위치 |
|----|--------|-----------|
| Windows | `schtasks.exe` | 작업 스케줄러 |
| macOS | `launchd` | `~/Library/LaunchAgents/com.gdrive-sync.*.plist` |
| Linux | `crontab` | 사용자 crontab |

```bash
# 매일 12시에 점심 동기화
gdrive-sync schedule add --name 점심 --type daily --time 12:00 --options "--no-limit"

# 평일 18:30 퇴근 후
gdrive-sync schedule add --name 퇴근후 --type weekly --time 18:30 \
    --weekdays mon,tue,wed,thu,fri --options "--no-limit"

# 15분마다 체크
gdrive-sync schedule add --name 15분체크 --type interval --interval 15

# 등록된 작업 보기
gdrive-sync schedule list

# 제거
gdrive-sync schedule remove 점심
gdrive-sync schedule remove --all
```

config.yaml에 `scheduler.jobs`를 미리 정의해두고 한 번에 등록:
```bash
gdrive-sync schedule install-from-config
```

**Mac 전용 참고**: 최초 등록 시 macOS가 "백그라운드에서 실행 허용" 권한을 물을 수 있습니다. 시스템 설정 → 일반 → 로그인 항목 → 백그라운드에서 허용에서 확인하세요.

### FreeFileSync 기법 참고 사항

이 v2는 FreeFileSync의 핵심 속도 기법을 참고했습니다:
- **병렬 스트림**: FreeFileSync 기본 1개, 우리는 기본 5개 (`performance.parallel_transfers`)
- **Simple/Resumable 분기**: 5MB 미만은 single POST, 그 이상은 resumable + 청크
- **큰 청크 크기**: 기본 8MB (FreeFileSync와 동일 수준)
- **연결 재사용**: httplib2 기본 keep-alive + 스레드별 Http 인스턴스

아직 채택 안 한 것: 이동/이름변경 감지 (폴더 이동만으로 큰 파일 재업로드 방지). 향후 검토 예정.

## 방화벽 환경 대응

현재 테스트에서는 직접 연결이 가능하지만, 추후 차단 시:

```yaml
network:
  use_proxy: true
  proxy_type: "http"       # http | socks4 | socks5
  proxy_host: "proxy.company.co.kr"
  proxy_port: 8080
  proxy_username: ""
  proxy_password: ""
```

SSH 터널을 통한 SOCKS5 우회:
```bash
ssh -D 1080 user@외부서버            # 터널 생성
# config.yaml에서 socks5://127.0.0.1:1080 로 지정
```

`gdrive-sync test-connection`으로 각 방식별 결과를 확인할 수 있습니다.

## 멀티 디바이스 사용법 (GitHub + Claude Code)

```bash
# [최초] GitHub 저장소 생성 후
cd gdrive-sync
git init
git add .
git commit -m "Initial"
git remote add origin https://github.com/<user>/gdrive-sync.git
git push -u origin main

# [새 기기에서] 이어서 개발
git clone https://github.com/<user>/gdrive-sync.git
cd gdrive-sync
pip install -e .
# credentials.json을 USB 등 안전한 방법으로 복사
gdrive-sync auth

# Claude Code에서 이 폴더를 열면 CLAUDE.md를 자동으로 읽어 컨텍스트 파악
```

**절대 commit 금지**: `credentials.json`, `token.json`, `config.yaml`, `.gdrive_sync_state.json` (`.gitignore`에 등록됨)

## 동기화 로직 상세

`sync_engine.py`가 9가지 시나리오를 판정합니다:

| 이전 상태 | 로컬 | 리모트 | 액션 |
|-----------|------|--------|------|
| 없음 | 있음 | 없음 | UPLOAD_NEW |
| 없음 | 없음 | 있음 | DOWNLOAD_NEW |
| 없음 | 있음 | 있음 | MD5 비교 → SKIP 또는 CONFLICT |
| 있음 | 변경 | 동일 | UPLOAD_UPDATE |
| 있음 | 동일 | 변경 | DOWNLOAD_UPDATE |
| 있음 | 변경 | 변경 | CONFLICT (정책 적용) |
| 있음 | 없음 | 있음 | DELETE_REMOTE |
| 있음 | 있음 | 없음 | DELETE_LOCAL |
| 있음 | 없음 | 없음 | REMOVE_STATE |

변경 감지: `size` + `mtime` + `md5` (Google Workspace 네이티브 파일은 제외).

## Google Workspace 네이티브 파일

Google Docs/Sheets/Slides 등은 **동기화 제외** 대상입니다 (binary 표현이 없어 MD5 비교 불가).
필요 시 브라우저에서 "다운로드 → Office 형식"으로 변환해야 합니다.

## 테스트

```bash
pip install -e ".[dev]"
pytest
```

## 라이선스

MIT
