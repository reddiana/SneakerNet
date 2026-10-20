# SneakerNet 작업 메모

## 목적
`pom.xml` / `build.gradle` / 단일 좌표(`group:artifact:version`)의 종속성을 하나의 디렉토리로 수집해 폐쇄망 Nexus 3(maven2 hosted)에 올리고,
올라간 Nexus에서 **Maven과 Gradle이 모두** 내려받을 수 있게 한다. 구현은 Python 3 표준 라이브러리 + Gradle init script.
같은 흐름으로 npm 패키지, PyPI 패키지, Docker 이미지도 받아 Nexus의 npm / pypi / docker hosted 저장소에 올린다 (2026-10-14 추가).

## 확정된 요구사항
- 입력: 빌드 파일(`pom.xml` 또는 `build.gradle[.kts]`, 여러 개 가능, 이름 자유 - 확장자로 구분) + 단일 좌표 모드
- 단일 좌표가 저장소에 없으면(`collect.check_coordinates`, 모든 저장소가 404) 다운로드 전에 분명한 오류로 끝낸다
  (없는 버전이면 최근 버전 목록, 없는 라이브러리면 철자 확인 안내). 오류 줄은 GUI에서 빨갛게 보이도록 모두 `오류:`로 시작한다.
- 빌드 파일 해석이 실패하면 `collect.explain_failure`가 Maven/Gradle 로그에서 "찾지 못한" 좌표(`not_found_gavs`)와
  pom에 버전까지 적힌 좌표(`pom_literal_gavs`, Maven은 첫 실패만 알려 주므로)를 저장소에 직접 확인해 같은 형식으로 보여 준다
  (파일의 줄 번호 포함). 저장소에 없는 것이 확인되지 않으면 원래 오류(로그 끝부분)를 그대로 낸다.
- Nexus는 범용 저장소: 어느 도구로 입력하든 Maven/Gradle 양쪽에서 해석되도록 최대한 보강
- 저장소: Maven Central, Google, Gradle Plugin Portal (SNAPSHOT, 사내 저장소 제외)
- 플랫폼: `linux-x86_64`, `windows-x86_64` (`--platforms`로 확장)
- 수집 파일: jar, pom, `.module`, 탈락 버전의 POM/jar (sources, javadoc, Gradle Wrapper 제외)
- 반입: 매번 전체 반입, 체크섬 매니페스트/증분/라이선스 점검 없음 (`_artifacts.csv`만 동봉)
- npm / PyPI / Docker (2026-10-14, 입력 형식과 GUI 배치는 사용자 확인 없이 내가 정했다. 사용자가 보고 바꿀 수 있다)
  - 입력: npm 은 `package-lock.json`/`package.json`(`.json`) + `이름@버전`, PyPI 는 `requirements.txt`(`.txt`) + `이름==버전`,
    Docker 는 `이름:태그`. 직접 적은 것이 저장소에 없으면 Maven 좌표와 같은 형식의 `오류:` 로 다운로드 전에 끝낸다.
  - 반입 디렉토리의 `_npm/`, `_pypi/`, `_docker/` 에 둔다 (`_` 로 시작해 Maven 업로드 대상과 섞이지 않는다).
  - upload 는 폴더에 들어 있는 종류마다 대상(`--repo`, `--npm-repo`, `--pypi-repo`, `--docker-url`)이 있어야 하고, 없으면 올리기 전에 오류. `--kinds` 로 고른 종류만 올리면 나머지 대상은 필요 없다.
  - PyPI 는 wheel 만 (대상 플랫폼 × `--python-versions`), Docker 는 linux 이미지만. docker 프로그램은 쓰지 않는다.
  - Docker 이미지는 `docker load -i` 로 바로 읽히는 tar 로 받는다 (사용자 요구, 2026-10-14). 이미지×플랫폼마다
    `_docker/<이름>_<태그>_<os>-<arch>.tar` 하나, 형식은 Docker 25+ `docker save` 와 같다 (OCI 레이아웃 + manifest.json).
    업로드도 이 tar 에서 블롭을 꺼내 올린다. 이 PC 에 docker 가 없어 실제 `docker load` 는 해 보지 못했고,
    `tests/helpers.py` 의 `check_docker_archive` 가 docker load 의 검사(diff_ids, digest)를 흉내 낸다.
  - verify 는 Maven / Gradle 만 한다.
- 단계: collect → upload → verify (업로드/검증은 폐쇄망에서 Python만으로 동작)
- GUI (사용자 요구로 확정된 것)
  - 화면 문구는 영어. 설명 없이 쓸 수 있도록 탭마다 안내문, 입력란마다 회색 설명을 둔다. 새 입력란을 추가할 때도 지킨다.
  - 최상위 탭이 종류(Maven / Gradle, npm, Python, Docker), 그 안에 Step 탭 (2026-10-20 사용자 요구). 각 Step 에는 그 종류에
    필요한 칸만 둔다. Step 3 은 Maven / Gradle 에만. Step 1 은 그 종류만 받고, Step 2 는 `upload --kinds <종류>` 로 그 종류만 올린다.
    zip 의 bat(`--step 2`)은 `bundle_kind` 로 폴더에 든 종류의 탭을 연다.
  - Step 1: `Start download`는 라이브러리를 "다운로드" 폴더의 새 폴더(`sneakernet-<날짜_시각>`)에 받기만 한다 (위치를 묻지 않고,
    zip도 만들지 않는다). zip은 `Create transfer zip...` 버튼을 눌렀을 때만 만들고, 그때 저장 위치(USB 등)를 묻는다.
    이번 실행에서 받은 것이 없으면 이전 다운로드 폴더를 먼저 고르게 한다.
    다운로드가 끝나면 그 폴더를 탐색기로 연다. Docker 이미지만 받았으면 `_docker` 폴더를 연다 (tar 만 가져가면 되어 zip 이 필요 없다).
    zip이 만들어지면 그 폴더를 파일 탐색기로 연다 (zip 선택 상태).
    (다운로드 후 자동 zip 생성 + 위치 입력란 방식은 한 번 만들었다가 사용자가 되돌렸다. 다시 제안하지 말 것.)
  - Nexus 주소/저장소/계정과 zip 위치는 `%APPDATA%\sneakernet\settings.json`에 저장. 비밀번호는 저장하지 않는다.
  - 반입용 zip의 bat은 Step 2 탭부터 연다 (`--step 2`). 프로젝트 폴더의 bat은 Step 1부터.
  - 로그창은 가로 스크롤 없이 줄바꿈.

## 구조
- `sneakernet_gui.py` tkinter GUI. CLI를 하위 프로세스로 실행하고 출력을 로그창에 보여 준다.
  - `write_transfer_zip`: 프로그램(`sneakernet/`) + 수집 결과(`bundle/`) + `Start sneakernet.bat` + `READ ME FIRST.txt` 를 zip 하나로
  - `find_bundle`: 프로그램 주변에서 `_artifacts.csv`가 있는 폴더를 찾아 Step 2에 채움
  - `find_tool`: PATH에 없는 mvn/gradle을 흔한 설치 위치에서 탐색 (Step 1, 3에 `--mvn-cmd`/`--gradle-cmd`로 전달)
- `Start sneakernet.bat` 이 PC에서 GUI 실행 (zip 안의 bat은 `sneakernet_gui.py`의 `START_BAT`에서 생성하며 내용이 다르다)
- `sneakernet.py` CLI 진입점, `snk/` 패키지 (`collect`, `maven`, `gradle`, `normalize`, `enrich`, `upload`, `verify`, `common`)
  - `collect.run` 이 종류별로 나눠 부른다: `_collect_java`(Maven/Gradle), `npm.collect`, `pypi.collect`, `docker.collect`
  - `snk/npm.py` lock 파일의 tarball 을 받는다 (lock 이 없으면 `npm install --package-lock-only`)
  - `snk/pypi.py` + `snk/pip_target.py` `pip download` 를 대상 플랫폼의 환경 마커로 실행 (`python -m snk.pip_target`, cwd 는 프로그램 폴더)
  - `snk/docker.py` 레지스트리 v2 클라이언트(`Registry`), `pull` / `write_archive`(tar 생성) / `push`
- `snk/init-resolve.groovy` 모든 resolvable configuration 강제 해석, `init-nexus.groovy` verify 용 저장소 바꿔치기
- `repos.json` 저장소, 플랫폼, `native_hints` 설정. `npm_registry`, `pypi_index`, 플랫폼별 `pip_platforms` / `docker_platform` 도 여기 있다.
- `tests/` 단위 테스트 + 가짜 Maven 저장소/가짜 Nexus/가짜 mvn 기반 end-to-end 테스트 (GUI 테스트는 없음)
  - `tests/test_packages.py` npm / PyPI / Docker (가짜 npm 저장소, 가짜 PyPI 색인 + 실제 pip, `FakeRegistry`)
- `tests/data/pom.xml`, `tests/data/mvn_build_result.txt` 는 테스트가 아니라 사용자가 문제 분석용으로 넣어 둔 자료다.

## 테스트
```bash
python -m unittest discover -s tests -t .   # 36개, 약 2분 (gradle 필요; Gradle 9 는 JDK 17+)
```

## 실제 환경에서 확인한 것 (2026-10-13, Windows 11, Maven 3.9.16, Gradle 9.8, JDK 17, Python 3.15)
- 실제 `mvn`으로 `samples/pom.xml` collect 통과. `parse_tree`는 실제 `tree -Dverbose` 출력(충돌 포함)과 맞는다.
- 실제 Gradle로 `samples/build.gradle` collect 통과 (106초, 491 GAV).
- 네이티브: netty epoll/tcnative, JavaFX 좌표에서 linux/windows classifier 파일 수집 확인.
- JDK 12+ 에서 Maven에 `-Dos.name`을 다른 OS로 주면 첫 HTTP 연결에서 죽는다(`jdk/net/LinuxSocketOptions`).
  `maven._run_mvn`이 이를 감지해 `os.name`/`os.arch` 없이 재실행한다. 이때 `<os>` 프로필은 호스트 기준이다. Gradle 쪽은 문제없었다.
- 오래된 POM의 HTML 엔티티(`&oslash;` 등)는 `enrich.parse_pom`이 숫자 참조로 바꿔 다시 읽는다.
- 실제 Nexus 3(사용자의 폐쇄망)에 `upload` 통과 (810개 업로드, 실패 0).
- 사용자의 실제 프로젝트(Keycloak 23.0.0 의존 `basic-auth-flow`) pom으로 collect 통과 (648 GAV, 문제/경고 없음).
- GUI: 코드로 구동해 실제 수집, zip 생성, zip을 풀어 bat 실행(`--step 2`)까지 확인.

## 실제 환경에서 확인한 것 (2026-10-14, npm 11.19, pip 26.2.1)
- 실제 npm 저장소: `--npm esbuild@0.21.5` 로 lock 생성 후 linux-x64 / win32-x64 것만 받음.
- 실제 PyPI: Windows PC 에서 linux + windows, Python 3.12 대상으로 `pywin32; sys_platform == 'win32'` 는 windows 용만,
  `uvloop; sys_platform != 'win32'` 는 linux 용만, click 의 colorama 는 windows 쪽에서만 받음 (마커 바꿔치기 동작).
- 실제 Docker Hub: `hello-world:latest`, `alpine:3.20`, `nginx:1.27-alpine` 의 linux/amd64 를 받음 (토큰 인증, 블롭 리다이렉트).
  만든 tar 는 `check_docker_archive` 통과, 가짜 레지스트리로 push 도 통과.
- 이 PC 의 npm 설정(.npmrc)에는 지금 네트워크에서 닿지 않는 프록시가 들어 있다. `npm.make_lock` 이 네트워크 오류를 보면 프록시 없이 다시 실행한다.
  Python(urllib, pip)은 프록시 없이 바로 나간다.

## 아직 검증하지 못한 것 (이어서 할 일)
테스트에서 `mvn`은 스텁(`tests/helpers.py`의 `FAKE_MVN`)이다.
0. **npm / PyPI / Docker 를 실제 Nexus 에 올리는 것.** 가짜 서버로만 시험했다. 확인할 것: components API 의 필드 이름
   (`npm.asset`, `pypi.asset`)과 응답 코드, 재배포 금지 저장소의 400 문구, Docker 커넥터에 Basic 인증으로 올라가는지,
   Nexus 가 새로 만든 index 를 받아 주는지. 그리고 받은 tar 가 실제 `docker load` 로 읽히는지.
1. **폐쇄망 빌드 실패 원인 확정**: 사용자의 Docker 빌드(`mvn clean package -DskipTests -B -o`)가 실패했다.
   처음에는 다른 프로젝트(Keycloak 26.0.0)의 pom으로 수집했던 것이 원인 중 하나였고, 올바른 pom으로 다시 한 뒤에도 실패한다고 한다.
   새 오류 로그는 아직 받지 못했다. 후보: (a) `-o` 오프라인 모드, (b) 업로드한 hosted 저장소가 빌드가 쓰는 group(`maven-public`)의
   멤버가 아님, (c) 수집 PC와 빌드 이미지의 Maven 버전이 달라 기본 플러그인 버전이 다름. (c)는 추정일 뿐 관찰된 적이 없다.
2. 실제 Nexus를 상대로 한 `verify`(Maven 포함). 폐쇄망 PC에서 mvn/gradle이 PATH에 없어 실패했고, GUI에 위치 지정 칸을 추가한 뒤의
   결과는 아직 모른다. Maven verify 는 스텁으로도 시험하지 않았다.
3. 실제 프로젝트(Spring Boot, Kotlin 플러그인 등)의 `build.gradle`로 collect
4. Maven 테스트 provider(surefire 등)가 빠지는지 확인
5. 2026-10-14 GUI 를 코드로 구동한 시험에서 단일 좌표 `junit:junit:4.13.2` (+ `hello-world` 이미지) collect 가
   10분 넘게 끝나지 않았다 (Maven 단계로 보임). 원인은 조사하지 않았다. 같은 시험을 npm 으로 바꾸면 바로 끝났다.
6. GUI 화면 배치. 이 PC는 화면 캡처가 차단되어 있어 직접 보지 못했다 (캡처를 다시 시도하지 말 것). 사용자가 보고 알려 준다.
   2026-10-20 종류 탭 → Step 탭 구조로 바꾼 화면은 코드로 구동해 명령 인자만 확인했고, 사용자는 아직 보지 않았다.

## 알려진 한계
- 빌드 파일을 하나씩 독립적으로 해석하므로 상대경로 parent, 멀티모듈, `apply from:` 등은 실패할 수 있음 (경고만 출력)
- 탈락 버전은 POM/jar/.module 까지만 수집 (그 버전의 전이 의존성은 교차 해석 범위에서만)
- `pom.xml`에 버전이 없는 기본 플러그인은 수집한 PC의 Maven 버전 기준으로 받는다
- Python 3.13, 3.15 에서만 테스트함

## 작업 시 주의
- 테스트가 실패하면 먼저 `logs/` (collect 의 `--work-dir`)와 `_report.txt` 를 본다.
- Gradle init script 의 lenient 해석은 다운로드 실패를 삼킨다. 실패는 `artifactView(...).artifacts.failures` 로 별도 기록한다 (과거 결함).
- 다운로드한 아티팩트가 담긴 디렉토리에서 스크립트를 실행하지 말 것. 경로는 인자로 넘긴다.
  같은 이유로 반입용 zip은 프로그램을 `bundle/` 안이 아니라 옆(`sneakernet/`)에 둔다.
- Nexus는 폐쇄망에만 있다. upload/verify/빌드 결과는 사용자가 알려 줘야 하고, GUI를 고치면 zip을 다시 만들어 반입해야 반영된다.
- 이 PC의 기본 `JAVA_HOME`은 사용자 변수로 JDK 17(`%USERPROFILE%\.jdks\azul-17.0.18`)이다 (시스템 변수는 JDK 11).
- GUI를 고친 뒤에는 떠 있는 창을 닫고 다시 열어야 반영된다.
