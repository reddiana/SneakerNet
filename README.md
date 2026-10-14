# SneakerNet

`pom.xml` / `build.gradle` / 단일 좌표의 종속성을 **하나의 디렉토리**로 모아 폐쇄망 Nexus에 올리고,
올라간 Nexus에서 **Maven과 Gradle이 모두** 내려받을 수 있게 만드는 도구입니다. Python 3 표준 라이브러리만 사용합니다.

같은 방식으로 **npm 패키지, Python(PyPI) 패키지, Docker 이미지**도 함께 받아 Nexus의 npm / pypi / docker 저장소에 올립니다.

```
[인터넷 PC]  Step 1 다운로드 ──► zip 하나 ── 반입(USB 등) ──►  [폐쇄망]  Step 2 업로드 ──► Nexus ──► Step 3 확인
```

## 준비물

| 단계 | 어디서 | 필요한 것 |
|---|---|---|
| Step 1 다운로드 (collect) | 인터넷 PC | Python 3. 받을 종류에 따라: JDK + `mvn` + `gradle` (Maven/Gradle), `npm` (lock 파일이 없을 때), `pip` (Python 패키지) |
| Step 2 업로드 (upload) | 폐쇄망 PC | Python 3 만 |
| Step 3 확인 (verify, 선택) | 폐쇄망 PC | Python 3, JDK, `mvn` 및/또는 `gradle` |

- **Python**: 3.13, 3.15에서 시험했습니다. 추가 패키지 설치는 없습니다.
- **JDK**: Gradle 9 이상은 JDK 17 이상에서만 실행됩니다. `JAVA_HOME`이 JDK 17+를 가리키게 하세요.
- **mvn / gradle**: Step 1은 입력 종류에 맞는 도구 하나만 있어도 되지만, 둘 다 있어야 반대 도구용 교차 보강이 됩니다.
- **Nexus**: Repository 3의 **maven2 (hosted)** 저장소가 미리 있어야 합니다. Version policy는 Release,
  Deployment policy는 "Allow redeploy"를 권장합니다 (재배포가 금지돼 있어도 이미 있는 파일은 건너뛰고 계속 진행합니다).
  `proxy`, `group` 타입 저장소에는 올릴 수 없습니다.
- **npm / Python / Docker**를 올리려면 종류마다 hosted 저장소가 따로 있어야 합니다: **npm (hosted)**, **pypi (hosted)**,
  **docker (hosted)**. Docker 저장소에는 HTTP 커넥터 포트(예: 8082)를 열어 두고, 그 주소(`http://nexus:8082`)를 따로 입력합니다.
- **Docker 프로그램은 어디에도 필요 없습니다.** 이미지는 레지스트리 API로 직접 받고 올립니다.

## 사용법 (GUI)

Windows에서는 `Start sneakernet.bat`을 더블클릭합니다. 그 밖에는 다음과 같이 실행합니다.

```bash
python sneakernet_gui.py
```

탭 세 개가 작업 순서입니다. 각 입력란 아래에 설명이 있고, 아래쪽 로그 창에 진행 상황이 나옵니다.

### Step 1: Download — 인터넷 되는 PC에서

받을 것의 종류마다 탭이 있습니다 (`Maven / Gradle`, `npm`, `Python (pip)`, `Docker images`). 필요한 탭만 채우면 되고,
`Start download` 한 번으로 네 탭의 내용을 모두 받습니다.

| 탭 | 파일로 넣기 | 한 줄에 하나씩 적기 |
|---|---|---|
| Maven / Gradle | `pom.xml`, `build.gradle[.kts]` | `group:artifact:version` |
| npm | `package-lock.json` (권장) 또는 `package.json` | `이름@버전` (예: `express@4.19.2`) |
| Python (pip) | `requirements.txt` (이름은 자유, `.txt`) | `이름==버전` (예: `requests==2.32.3`) |
| Docker images | — | `이름:태그` (예: `nginx:1.27`, `ghcr.io/org/app:1.0`) |

- **npm**: `package-lock.json`이 있으면 거기 적힌 버전 그대로 받고 `npm`이 필요 없습니다. `package.json`만 넣으면 같은 폴더의
  lock 파일을 쓰고, 없으면 `npm`으로 버전을 정합니다 (설치는 하지 않습니다). 다른 운영체제 전용 패키지는 받지 않습니다.
- **Python**: `Python versions`에 **폐쇄망에서 쓸 Python 버전**(예: `3.11`)을 적습니다. 그 버전과 선택한 운영체제용 wheel을
  의존성까지 받습니다. wheel이 없고 소스 배포본만 있는 패키지는 받지 못하고 오류로 알려 줍니다.
- **Docker**: Linux 이미지를 받습니다 (`linux-x86_64` → linux/amd64, `linux-aarch64` → linux/arm64).
  Nexus에는 원래 레지스트리 주소를 뺀 이름으로 올라갑니다 (`nginx:1.27`, `org/app:1.0`).
- 어느 종류든 직접 적은 이름이나 버전이 저장소에 없으면 다운로드를 시작하지 않고 오류로 알려 줍니다.

아래는 `Maven / Gradle` 탭 기준 설명입니다.

1. `Project build files`의 `Add file...`로 프로젝트의 `pom.xml` 또는 `build.gradle[.kts]`를 추가합니다.
   **폐쇄망에서 실제로 빌드할 바로 그 파일**이어야 합니다 (버전이 하나만 달라도 다른 라이브러리가 필요합니다).
   - 여러 개를 한 번에 넣을 수 있고, 파일 이름은 자유입니다 (예: `pom23.xml`, `pom26.xml`). 확장자로 종류를 구분합니다
     (`.xml`/`.pom` → Maven, `.gradle`/`.gradle.kts` → Gradle). 각 파일은 서로 독립된 프로젝트로 해석됩니다.
   - 파일 없이 라이브러리만 받으려면 `Single libraries`에 `group:artifact:version`을 한 줄에 하나씩 적습니다.
     저장소에 없는 버전이나 이름을 적으면 다운로드를 시작하지 않고, 있는 최근 버전 목록과 함께 오류를 보여 줍니다.
   - `pom.xml`/`build.gradle` 안에 저장소에 없는 버전이나 이름이 있어도 같은 형식의 오류가 나오고, 파일의 몇 번째 줄인지 알려 줍니다.
2. `Target systems`에서 라이브러리를 쓸 운영체제를 고릅니다 (기본: linux-x86_64, windows-x86_64).
3. `Start download`를 누릅니다. 샘플 기준 2분 안팎이 걸립니다. 저장 위치는 묻지 않습니다.
4. `Open summary report`로 `_report.txt`를 열어 `[문제]`가 없는지 확인합니다.
5. `Create transfer zip...`을 누르고 zip을 저장할 곳(USB 드라이브 등)을 고릅니다. 마지막으로 고른 위치에서 시작합니다.
   zip이 만들어지면 그 폴더가 파일 탐색기로 열리고 zip이 선택되어 있습니다.

| 무엇 | 어디에 | 언제 |
|---|---|---|
| 내려받은 라이브러리 | "다운로드" 폴더의 `sneakernet-<날짜_시각>\` (매번 새 폴더) | `Start download` |
| Maven/Gradle 로그 | "다운로드" 폴더의 `sneakernet-<날짜_시각>-work\` | `Start download` |
| **반입용 zip** | 직접 고른 곳의 `sneakernet-<날짜_시각>-transfer.zip` | `Create transfer zip...` |

반입할 것은 **zip 파일 하나**입니다. 그 안에 이 프로그램과 라이브러리가 함께 들어 있습니다.
프로그램을 새로 켠 뒤 `Create transfer zip...`을 누르면 "다운로드" 폴더에서 이전에 받은 `sneakernet-<날짜_시각>` 폴더를 먼저 고르게 합니다.
`Open result folder`는 탐색기에서 zip(아직 없으면 다운로드 폴더)을 보여 줍니다.

### Step 2: Upload to Nexus — 폐쇄망 PC에서

1. zip을 폐쇄망 PC로 옮겨 압축을 풀고 `Start sneakernet.bat`을 더블클릭합니다.
2. `Step 2` 탭이 바로 열립니다. `Downloaded folder`는 압축 푼 곳의 `bundle` 폴더로 자동으로 채워집니다.
3. `Nexus address`, `User name`, `Password`와, 올릴 종류에 맞는 저장소를 넣습니다. 비밀번호를 뺀 값은 이 PC에 기억됩니다.
   - `Maven repository` (maven2 hosted), `npm repository` (npm hosted), `PyPI repository` (pypi hosted): 저장소 **이름**
   - `Docker registry address`: docker hosted 저장소의 커넥터 **주소** (예: `http://nexus:8082`). Nexus 주소와 다릅니다.
   - 폴더에 들어 있는 종류의 칸만 채우면 됩니다. 필요한 칸이 비어 있으면 올리기 전에 어느 칸인지 알려 줍니다.
4. 먼저 `Trial run`을 체크하고 `Start upload`를 눌러 주소·계정·저장소가 맞는지 확인합니다.
5. 체크를 풀고 `Start upload`를 다시 누릅니다. 로그 끝에 `업로드 N, 건너뜀 N, 실패 0`이 나오면 끝입니다.

### Step 3: Check — 폐쇄망 PC에서 (선택)

Nexus 하나만 바라보고 빈 캐시에서 원래 프로젝트가 해석되는지 확인합니다. Maven / Gradle 라이브러리만 확인합니다
(npm, Python, Docker는 아래 "폐쇄망에서 받아 쓰기"의 명령으로 직접 확인하세요).

1. `Sample projects folder`는 자동으로 채워집니다 (`bundle\_samples`).
2. `Maven program`, `Gradle program`은 PATH에 없어도 흔한 설치 위치에서 찾아 채워집니다.
   비어 있으면 `Choose...`로 설치 폴더의 `bin\mvn.cmd`, `bin\gradle.bat`을 지정합니다. 없는 도구는 `Check with`에서 체크를 풉니다.
3. 비밀번호를 넣고 `Start check`를 누릅니다.

### 알아 둘 것

- 설정 저장 위치: `%APPDATA%\sneakernet\settings.json` (Nexus 주소/저장소/계정, 마지막 zip 저장 위치). 비밀번호는 저장하지 않습니다.
- FAT32로 포맷된 USB는 4 GB를 넘는 파일을 담지 못합니다. zip이 그보다 크면 exFAT/NTFS USB를 쓰세요.
- 잘 안 쓰는 옵션(교차 해석 생략, 다운로드 폴더 직접 지정 등)은 Step 1의 `Show advanced options`에 있습니다.
- 반입이 끝나면 "다운로드" 폴더의 `sneakernet-...` 폴더들은 지워도 됩니다.

## 폐쇄망에서 받아 쓰기 (npm, Python, Docker)

```bash
npm install --registry http://nexus:8081/repository/npm-hosted/
pip install -r requirements.txt --index-url http://nexus:8081/repository/pypi-hosted/simple --trusted-host nexus
docker pull nexus:8082/nginx:1.27        # 업로드가 끝나면 로그에 이미지마다 이 명령이 나옵니다
```

`package-lock.json`의 `resolved`가 `registry.npmjs.org`를 가리키고 있어도 npm은 `--registry`로 준 주소에서 받습니다.
Docker 커넥터가 HTTP라면 Docker 데몬의 `insecure-registries`에 `nexus:8082`를 넣어야 합니다.

## 업로드는 됐는데 폐쇄망 빌드가 실패할 때

오류 문구로 원인을 구분합니다.

| 오류 문구 | 원인 | 조치 |
|---|---|---|
| `Cannot access ... in offline mode` | 빌드 명령에 `-o`(오프라인)가 있어 Nexus에 접속하지 않음 | 빌드 명령(Dockerfile 등)에서 `-o`를 뺌 |
| `Could not find artifact ... in <저장소>` | Nexus에 그 버전이 없거나, 빌드가 바라보는 저장소에서 보이지 않음 | 아래 두 가지를 확인 |
| `Plugin ...:maven-xxx-plugin:x.y.z ... could not be resolved` | 수집한 PC와 빌드 환경의 Maven 버전이 달라 기본 플러그인 버전이 다름 | 같은 Maven 버전으로 수집하거나, `pom.xml`에 플러그인 버전을 명시 |

- **다른 pom으로 수집하지 않았는지**: 수집 폴더의 `_samples\`에 실제로 사용된 파일 사본이 있습니다. 빌드하는 프로젝트의
  artifactId·버전과 같은지 비교하세요.
- **group 저장소 멤버인지**: 빌드가 `maven-public` 같은 group 저장소에서 받는다면, 업로드한 hosted 저장소가 그 group의
  멤버여야 합니다. 브라우저에서 `<Nexus>/repository/<group 이름>/<그룹 경로>/<artifact>/<버전>/<artifact>-<버전>.pom`을 열어
  내용이 보이는지 확인하면 됩니다.

## 사용법 (명령줄)

GUI는 아래 명령을 그대로 실행합니다. 자동화하거나 GUI를 쓸 수 없을 때 직접 쓰세요.

```bash
# 1) 인터넷 PC: 수집
python sneakernet.py collect --input pom.xml --out ./bundle
python sneakernet.py collect --input build.gradle --input pom.xml --out ./bundle      # 여러 개도 가능
python sneakernet.py collect --coordinate org.springframework:spring-core:6.1.14 --out ./bundle
python sneakernet.py collect --input package-lock.json --input requirements.txt --python-versions 3.11 \
                             --image nginx:1.27 --npm express@4.19.2 --pypi requests==2.32.3 --out ./bundle

# 2) 프로그램 폴더와 ./bundle 디렉토리를 폐쇄망으로 반입

# 3) 폐쇄망: 업로드 (비밀번호는 환경 변수 NEXUS_PASSWORD 또는 프롬프트 입력)
python sneakernet.py upload --dir ./bundle --url http://nexus:8081 --repo maven-hosted --user admin
python sneakernet.py upload --dir ./bundle --url http://nexus:8081 --user admin \
                            --npm-repo npm-hosted --pypi-repo pypi-hosted --docker-url http://nexus:8082

# 4) 폐쇄망: 검증
python sneakernet.py verify --url http://nexus:8081 --repo maven-hosted --user admin --samples ./bundle/_samples
```

### collect 옵션

| 옵션 | 설명 |
|---|---|
| `--input FILE` | `pom.xml`, `build.gradle[.kts]`, `package.json` / `package-lock.json`(`.json`), `requirements.txt`(`.txt`) (여러 번 지정 가능) |
| `--coordinate G:A:V` | 단일 종속성 (여러 번 지정 가능). Maven과 Gradle이 각자 해석 |
| `--npm NAME@VERSION` | npm 패키지 (여러 번 지정 가능) |
| `--pypi NAME==VERSION` | Python 패키지 (여러 번 지정 가능) |
| `--python-versions` | Python 패키지를 쓸 Python 버전, 쉼표 구분 (기본: 실행한 Python의 버전) |
| `--image NAME:TAG` | Docker 이미지 (여러 번 지정 가능) |
| `--out DIR` | 결과 디렉토리 (비어 있어야 함. `--force`를 주면 비우고 다시 만듦) |
| `--platforms` | 기본 `linux-x86_64,windows-x86_64`. `repos.json`의 `platforms`에 정의된 이름 사용 |
| `--repos-config` | 저장소/플랫폼/네이티브 힌트 설정 (기본 `repos.json`: Central, Google, Gradle Plugin Portal) |
| `--mvn-cmd`, `--gradle-cmd`, `--npm-cmd` | PATH에 없는 `mvn` / `gradle` / `npm`의 경로 |
| `--maven-goals` | 추가로 실행할 Maven 목표, 예: `"verify -DskipTests"` |
| `--no-cross` | 반대 도구용 교차 해석 생략 (빠르지만 덜 완전) |
| `--no-losers` | 충돌로 탈락한 버전 수집 생략 |
| `--work-dir`, `--keep-work` | 작업 디렉토리와 도구별 로그(`logs/`) 보존 |
| `--no-proxy` | 환경 변수의 HTTP 프록시 무시 |

### upload / verify 옵션

| 옵션 | 설명 |
|---|---|
| `upload --repo`, `--npm-repo`, `--pypi-repo` | maven2 / npm / pypi hosted 저장소 이름 (폴더에 들어 있는 종류만 필요) |
| `upload --docker-url` | docker hosted 저장소의 레지스트리 주소, 예: `http://nexus:8082` |
| `upload --check-exists` | 올리기 전에 존재 여부를 확인해 이미 있는 파일은 건너뜀 |
| `upload --dry-run` | 대상 파일만 세고 올리지 않음 |
| `upload --report FILE` | 결과 보고서 경로 (기본 `upload-report.json`) |
| `verify --tools maven,gradle` | 검증할 도구 (설치된 것만 지정) |
| `verify --offline-build` | Maven은 해석 후 `-o package -DskipTests`까지 수행 |
| `verify --mvn-cmd`, `--gradle-cmd` | PATH에 없는 `mvn` / `gradle`의 경로 |

### 산출 디렉토리

```
bundle/
├── com/example/lib/1.0/lib-1.0.jar|pom|module ...   ← Maven2 레이아웃 (Nexus에 올라가는 부분)
├── _npm/<이름>/<이름>-<버전>.tgz                      ← npm 패키지
├── _pypi/<이름>-<버전>-...whl                         ← Python 패키지
├── _docker/images.json, _docker/blobs/sha256/<hex>   ← Docker 이미지 (매니페스트, 설정, 레이어)
├── _artifacts.csv      ← 파일 목록(경로, 크기, 출처)
├── _report.txt         ← 요약, 경고, 문제
└── _samples/           ← verify 에 쓰는 입력 사본
```

`_` 로 시작하는 항목은 Maven 저장소에 올라가지 않습니다 (`_npm`, `_pypi`, `_docker`는 각자의 저장소로 올라갑니다). 체크섬(`.sha1` 등)과 `maven-metadata*.xml`은 Nexus가 직접 만들기 때문에 넣지 않습니다.

## 동작 방식

1. **격리 해석**: 항상 빈 캐시(`-Dmaven.repo.local`, 임시 `GRADLE_USER_HOME`)에서 실제 `mvn` / `gradle`을 실행합니다.
   - Maven: `maven-dependency-plugin:3.8.1:go-offline`(의존성과 플러그인) + `tree -Dverbose`(탈락 버전 파악)
   - Gradle: init script가 compile/runtime/test/annotationProcessor/buildscript 등 **모든 resolvable configuration**을 강제 해석하고, 받지 못한 jar는 실패로 처리
2. **정규화**: Gradle 캐시(해시 디렉토리)를 Maven2 레이아웃으로 변환하고, 잡파일을 제거
3. **교차 해석**: 입력하지 않은 쪽 도구로 직접 의존성과 탈락 버전을 해석 (Maven 입력 → Gradle, Gradle 입력 → Maven).
   Maven은 nearest-wins, Gradle은 highest-wins라 같은 입력에서도 선택 버전이 다를 수 있기 때문입니다.
4. **보강**: POM(parent, import된 BOM 재귀), Gradle `.module`과 그 안의 같은 디렉토리 파일(플랫폼 필터 적용, sources/javadoc 제외),
   대표 아티팩트(jar 등), 탈락 버전, `repos.json`의 `native_hints`에 해당하는 플랫폼별 classifier 파일을 원격에서 채움 (SHA-1 검증)
5. **업로드**: jar 등 → `.pom` → `.module` → `.asc` 순으로 HTTP PUT. 인증 오류는 즉시 중단, 5xx는 재시도

### npm, Python, Docker

- **npm**: lock 파일의 `resolved` 주소에서 tarball을 받아 `integrity`를 확인합니다. lock 파일이 없으면
  `npm install --package-lock-only`로 lock 파일만 만듭니다. `os` / `cpu` 제한이 있는 패키지는 선택한 플랫폼 것만 받습니다.
- **Python**: 플랫폼과 Python 버전마다 `pip download --only-binary=:all: --platform ... --python-version ...`를 실행합니다.
  pip은 환경 마커(`; sys_platform == "win32"` 등)를 수집 PC 기준으로 평가하므로, 대상 플랫폼 기준으로 평가하도록
  바꿔서 실행합니다 (`snk/pip_target.py`).
- **Docker**: 레지스트리 API로 매니페스트와 블롭을 받아 sha256을 확인합니다. 여러 플랫폼을 받았으면 받은 것만 담은
  목록(index)을 새로 만들어 올립니다.
- **업로드**: npm / Python은 Nexus의 components API(`/service/rest/v1/components`), Docker는 레지스트리 API로 올립니다.

## 한계 (알아 두세요)

- **Python 소스 배포본**(wheel이 없는 패키지)은 받지 않습니다. **npm의 git / 파일 경로 의존성**도 받지 않습니다 (경고로 알림).
- **Docker**: 로그인이 필요한 비공개 이미지와 Windows 컨테이너 이미지는 받지 않습니다. 여러 플랫폼을 받으면 목록을 새로 만들기
  때문에 태그의 digest가 원본과 다릅니다 (`이미지@sha256:...`로 고정해 쓰는 경우 주의. 플랫폼 하나만 받으면 원본과 같습니다).
- npm / Python / Docker는 Step 3(확인)의 대상이 아닙니다.

- **파일 하나만 입력**하므로 상대경로 parent, 멀티모듈, `apply from:` 등에 의존하는 파일은 해석에 실패할 수 있습니다.
  같은 위치의 `settings.gradle`(include 없는 경우), `gradle.properties`, `gradle/*.toml`은 자동으로 함께 사용합니다. 위험 패턴은 경고로 알려 줍니다.
- **네이티브 라이브러리**: Gradle/Maven을 해당 플랫폼처럼 실행하도록 `os.name` 등을 덮어쓰는 best-effort 방식이고,
  나머지는 `native_hints`와 `.module` 파일명 기반 필터로 보강합니다. 목록에 없는 라이브러리는 `repos.json`에 추가하세요.
  JDK 12 이상에서는 Maven에 다른 OS의 `os.name`을 줄 수 없어(첫 다운로드에서 죽음) 자동으로 덮어쓰기 없이 다시 실행합니다.
  이때 POM의 `<os>` 프로필은 수집하는 PC 기준으로만 켜지므로, 다른 OS용 파일은 `native_hints`에 의존합니다.
- **탈락 버전은 POM/jar/.module까지만** 받습니다. 그 버전의 전이 의존성은 교차 해석이 닿는 범위에서만 포함됩니다.
- **Maven 테스트 provider**(surefire-junit-platform 등)는 테스트가 실제로 실행될 때 해석되므로 `go-offline`만으로는 빠질 수 있습니다.
  이런 경우 `--maven-goals "verify"` (GUI: advanced options의 `Extra Maven goals`)로 실제 빌드를 한 번 돌려 받아 두세요.
- SNAPSHOT, 인증이 필요한 사내 저장소, Gradle Wrapper 배포본, sources/javadoc은 포함하지 않습니다.
- 라이선스/보안 점검은 하지 않습니다 (`_artifacts.csv`를 점검 도구에 넘길 수 있습니다).

## 테스트

```bash
python -m unittest discover -s tests -t .   # 36개, 약 2분 (gradle 과 JDK 17+ 필요)
```

가짜 Maven 저장소와 가짜 Nexus를 로컬에 띄워 enrich / upload / Gradle 수집 / verify를 end-to-end로 검증합니다.
Maven 수집은 플러그인 다운로드가 필요해서 테스트에서는 `mvn`을 대체하는 스텁을 사용합니다.
npm / Python / Docker도 가짜 저장소, 가짜 레지스트리, 가짜 Nexus로 수집과 업로드를 검증합니다 (`tests/test_packages.py`).
실제 `mvn`/`gradle` 수집과 실제 Nexus 3 업로드(Maven)는 별도로 확인했고, 실제 Nexus를 상대로 한 verify는 아직 확인하지 못했습니다.
npm / Python / Docker는 실제 저장소에서 받는 것까지 확인했고, **실제 Nexus에 올리는 것은 아직 확인하지 못했습니다.**
