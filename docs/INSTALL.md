# gdrive-sync 설치 안내 (받는 분용)

이 문서는 gdrive-sync 를 처음 설치하는 분을 위한 안내입니다.
Windows 기준으로 설명하며, 약 20~30분 정도 걸립니다.

전체 과정은 3단계입니다:

1. **프로그램 설치** — Python + Git 설치 후 명령어 한 줄
2. **Google 인증 키 발급** — 본인 Google 계정으로 credentials.json 만들기 (최초 1회)
3. **초기 설정** — 동기화할 폴더 지정

---

## 1단계. 프로그램 설치

### 1-1. Python 설치

1. https://www.python.org/downloads/ 에서 최신 버전 다운로드
2. 설치 첫 화면에서 **"Add python.exe to PATH" 체크박스를 반드시 체크**하고 Install Now
3. 설치 확인: 시작 메뉴에서 `cmd` 를 실행하고 아래 입력

```bash
python --version
```

`Python 3.x.x` 가 나오면 성공.

### 1-2. Git 설치

프로그램 설치와 업데이트에 필요합니다.

1. https://git-scm.com/download/win 에서 다운로드
2. 설치 중 옵션은 전부 기본값 그대로 "Next" 만 눌러도 됩니다

### 1-3. gdrive-sync 설치

`cmd` 창에서:

```bash
pip install git+https://github.com/GYcodeAI/gdrive-sync.git
```

> **저장소가 비공개인 경우**: GitHub 로그인 창이 뜹니다. 프로그램을 전달한
> 사람에게 ① GitHub 계정 아이디를 알려주고 저장소 접근 권한(초대)을 받은 뒤
> ② 본인 GitHub 계정으로 로그인하면 됩니다. 로그인은 최초 1회만 필요합니다.

설치 확인:

```bash
gdrive-sync --version
```

---

## 2단계. Google 인증 키(credentials.json) 발급

이 프로그램은 **본인의 Google 계정**으로 Drive 에 접근합니다. 이를 위해
Google Cloud Console 에서 무료로 "OAuth 클라이언트"를 하나 만들어야 합니다.
어렵게 들리지만 클릭 몇 번이면 됩니다. 비용은 전혀 들지 않습니다.

### 2-1. 프로젝트 만들기

1. https://console.cloud.google.com 접속 → Google 계정으로 로그인
2. 상단의 프로젝트 선택 드롭다운 → **새 프로젝트**
3. 프로젝트 이름: `gdrive-sync` (아무거나 가능) → **만들기**

### 2-2. Drive API 켜기

1. 왼쪽 메뉴 → **API 및 서비스** → **라이브러리**
2. "Google Drive API" 검색 → 클릭 → **사용** 버튼

### 2-3. OAuth 동의 화면 설정

1. **API 및 서비스** → **OAuth 동의 화면**
2. User Type: **외부(External)** 선택 → 만들기
3. 앱 이름: `gdrive-sync`, 사용자 지원 이메일·개발자 이메일: 본인 이메일 → 저장
4. 범위(Scopes) 단계: 그냥 **저장 후 계속** (프로그램이 알아서 요청합니다)
5. 테스트 사용자 단계: **+ ADD USERS** → 본인 Gmail 주소 추가 → 저장

### 2-4. OAuth 클라이언트 ID 만들기

1. **API 및 서비스** → **사용자 인증 정보(Credentials)**
2. **+ 사용자 인증 정보 만들기** → **OAuth 클라이언트 ID**
3. 애플리케이션 유형: **데스크톱 앱** → 만들기
4. 생성 완료 화면에서 **JSON 다운로드** 클릭

### 2-5. 파일 배치

다운로드된 파일(`client_secret_xxxx.json`)의 이름을 `credentials.json` 으로
바꾸고, 아래 폴더에 넣습니다:

```
C:\Users\<내계정>\.gdrive_sync\credentials.json
```

`.gdrive_sync` 폴더가 없으면 새로 만드세요. (탐색기 주소창에
`%USERPROFILE%` 입력 → 새 폴더 `.gdrive_sync` 생성)

### 2-6. (권장) 앱을 프로덕션으로 게시

위 상태("테스트 모드")로도 동작하지만, **로그인이 7일마다 만료**되어 매주
재인증해야 합니다. 이를 피하려면:

1. **OAuth 동의 화면** → **앱 게시(PUBLISH APP)** 버튼 → 확인
2. 이후 로그인 시 "Google 에서 확인하지 않은 앱" 경고가 나오면
   **고급 → gdrive-sync(안전하지 않음)으로 이동** 을 눌러 진행하세요.
   본인이 직접 만든 앱이므로 안전합니다.

---

## 3단계. 초기 설정 및 첫 동기화

`cmd` 창에서 순서대로:

```bash
gdrive-sync init
```

```bash
gdrive-sync auth
```

브라우저가 열리면 본인 Google 계정으로 로그인하고 **허용**을 누릅니다.

```bash
gdrive-sync config --edit
```

설정 파일이 열리면 동기화할 로컬 폴더와 Drive 폴더를 지정합니다.
(전달한 사람에게 예시 설정을 받아 붙여넣는 것이 가장 쉽습니다)

미리보기(실제 전송 없음)로 확인:

```bash
gdrive-sync sync --dry-run
```

문제없으면 실제 동기화:

```bash
gdrive-sync sync
```

### GUI 로 사용하기

명령어 대신 그래픽 화면을 쓰려면:

```bash
gdrive-sync gui
```

바탕화면 우클릭 메뉴에 등록하려면 (콘솔 창 없이 실행):

```bash
gdrive-sync context-menu
```

---

## 업데이트 방법

프로그램이 개선되면 새 버전 알림이 뜹니다 (GUI 시작 시 / 동기화 후).
아래 중 편한 방법으로 업데이트하세요:

- **명령어**: `cmd` 에서 `gdrive-sync update`
- **GUI**: 도움말 메뉴 → 업데이트 확인
- **더블클릭**: 같이 전달받은 `update.bat` 실행

---

## 문제 해결

| 증상 | 해결 |
|---|---|
| `pip` 또는 `python` 명령을 찾을 수 없음 | Python 재설치하며 "Add to PATH" 체크 |
| 설치 중 `git` 명령을 찾을 수 없음 | 1-2단계 Git 설치 |
| 설치 시 GitHub 인증 실패 | 전달한 사람에게 저장소 초대를 받았는지, 초대 메일을 수락했는지 확인 |
| `credentials.json 파일이 없습니다` | 2-5단계 파일 위치·이름 확인 |
| 로그인하면 "액세스 차단됨" | 2-3단계 테스트 사용자에 본인 이메일이 추가됐는지 확인 |
| 일주일마다 재로그인 요구 | 2-6단계 앱 게시 진행 |
| 그 외 오류 | `gdrive-sync -v sync` 로 실행한 화면을 캡처해서 전달한 사람에게 문의 |
