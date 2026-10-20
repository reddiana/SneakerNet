"""upload 서브커맨드: 반입 디렉토리를 Nexus Repository 3 의 hosted 저장소에 올린다.

Maven 레이아웃 파일은 maven2 저장소에 HTTP PUT, _npm / _pypi 는 components API, _docker 는 레지스트리 API 로 올린다.

표준 라이브러리만 사용하므로 폐쇄망 PC 에 별도 설치가 필요 없다.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from . import docker
from .common import BundlerError, HttpClient, is_junk, log

_SKIP_RE = re.compile(r"does not allow updating|cannot be updated|already exists", re.I)


class FatalUploadError(BundlerError):
    """계속 진행해도 의미가 없는 오류 (인증 실패, 저장소 없음 등)."""


def list_files(root: Path) -> list[tuple[str, Path]]:
    files = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or is_junk(f.name) or f.name.startswith("."):
            continue
        rel = f.relative_to(root)
        if rel.parts[0].startswith("_") or len(rel.parts) < 4:  # _artifacts.csv, _samples 등은 업로드 대상 아님
            continue
        files.append((rel.as_posix(), f))
    return files


def priority(rel: str) -> int:
    """jar 등 실제 아티팩트 → pom → .module → 서명(.asc) 순으로 올린다."""
    name = rel.lower()
    if name.endswith(".pom"):
        return 1
    if name.endswith(".module"):
        return 2
    if name.endswith(".asc"):
        return 3
    return 0


def preflight(client: HttpClient, url: str, wanted: dict[str, str]) -> None:
    """wanted: {저장소 이름: 형식(maven2 / npm / pypi)}. 저장소가 있고 형식이 맞는 hosted 인지 확인한다."""
    status, _ = client.get(f"{url}/service/rest/v1/status")
    if status != 200:
        log.warning("Nexus 상태 확인 실패 (HTTP %s). 주소가 올바른지 확인하세요: %s", status, url)
    status, body = client.get(f"{url}/service/rest/v1/repositories")
    if status == 200:
        repos = json.loads(body.decode("utf-8"))
        for repo, fmt in wanted.items():
            match = next((r for r in repos if r.get("name") == repo), None)
            if match is None:
                same = [r.get("name", "?") for r in repos if r.get("format") == fmt and r.get("type") == "hosted"]
                raise BundlerError(f"Nexus 에 저장소 {repo!r} 가 없습니다. {fmt} hosted 저장소: {', '.join(same) or '없음'}")
            if match.get("format") != fmt or match.get("type") != "hosted":
                raise BundlerError(f"저장소 {repo!r} 는 {fmt} hosted 가 아닙니다 "
                                   f"(format={match.get('format')}, type={match.get('type')}).")
    elif status in (401, 403):
        log.warning("저장소 목록 조회 권한이 없어 사전 점검을 건너뜁니다 (HTTP %s).", status)
    else:
        log.warning("저장소 목록 조회 실패 (HTTP %s) - 사전 점검을 건너뜁니다.", status)


def put_file(client: HttpClient, base: str, rel: str, path: Path, check_exists: bool) -> tuple[str, str]:
    url = base + quote(rel, safe="/")
    if check_exists and client.head(url) == 200:
        return "skipped", "이미 존재"
    size = path.stat().st_size

    def factory():
        fh = open(path, "rb")
        return fh, size, fh.close

    status, body = client.request("PUT", url, body_factory=factory,
                                  headers={"Content-Type": "application/octet-stream"})
    if status in (200, 201, 204):
        return "uploaded", ""
    msg = body.decode("utf-8", errors="replace").strip().replace("\n", " ")[:200]
    if status == 400 and _SKIP_RE.search(msg):
        return "skipped", "재배포 금지 저장소에 이미 존재"
    if status in (401, 403):
        raise FatalUploadError(f"HTTP {status}: 인증/권한 오류입니다. 계정과 저장소의 업로드 권한(nx-repository-view-maven2-*-add/edit)을 확인하세요.")
    if status == 404:
        raise FatalUploadError(f"HTTP 404: 저장소 경로를 찾을 수 없습니다 ({url}).")
    return "failed", f"HTTP {status} {msg}"


def post_component(client: HttpClient, url: str, repo: str, fmt: str, path: Path) -> tuple[str, str]:
    """npm tarball / Python 배포 파일을 components API 로 올린다 (Nexus 가 파일 안의 이름과 버전을 읽는다)."""
    boundary = "----sneakernet" + uuid.uuid4().hex
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="{fmt}.asset"; filename="{path.name}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    size = len(head) + path.stat().st_size + len(tail)

    def factory():
        fh = open(path, "rb")

        def parts():
            yield head
            while chunk := fh.read(1 << 20):
                yield chunk
            yield tail
        return parts(), size, fh.close

    status, body = client.request("POST", f"{url}/service/rest/v1/components?repository={quote(repo, safe='')}",
                                  body_factory=factory,
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    if status in (200, 201, 204):
        return "uploaded", ""
    msg = body.decode("utf-8", errors="replace").strip().replace("\n", " ")[:200]
    if status == 400 and _SKIP_RE.search(msg):
        return "skipped", "재배포 금지 저장소에 이미 존재"
    if status in (401, 403):
        raise FatalUploadError(f"HTTP {status}: 인증/권한 오류입니다. 계정과 저장소 {repo!r} 의 업로드 권한을 확인하세요.")
    if status == 404:
        raise FatalUploadError(f"HTTP 404: 저장소 {repo!r} 를 찾을 수 없습니다.")
    return "failed", f"HTTP {status} {msg}"


KINDS = {"maven": "Maven / Gradle 라이브러리", "npm": "npm 패키지", "pypi": "Python 패키지", "docker": "Docker 이미지"}


def run(args) -> int:
    root = Path(args.dir).resolve()
    if not root.is_dir():
        raise BundlerError(f"디렉토리가 없습니다: {root}")
    password = args.password or os.environ.get("NEXUS_PASSWORD")
    if args.user and password is None:
        if not sys.stdin.isatty():
            raise BundlerError("비밀번호가 필요합니다 (--password, 환경 변수 NEXUS_PASSWORD, 또는 터미널 입력).")
        password = getpass.getpass(f"{args.user} 비밀번호: ")

    url = args.url.rstrip("/")
    client = HttpClient(no_proxy=args.no_proxy, user=args.user, password=password)

    files = []
    for rel, path in list_files(root):
        if "-SNAPSHOT/" in rel:
            log.warning("SNAPSHOT 은 올리지 않습니다: %s", rel)
            continue
        files.append((rel, path))
    npm_files = sorted((root / "_npm").rglob("*.tgz"))
    pypi_files = sorted(f for f in (root / "_pypi").glob("*") if f.name.endswith((".whl", ".tar.gz", ".zip")))
    images = docker.load_images(root)
    if getattr(args, "kinds", None):   # GUI 는 종류별 탭에서 그 종류만 올린다
        kinds = {k.strip().lower() for k in args.kinds.split(",") if k.strip()}
        unknown = kinds - set(KINDS)
        if unknown:
            raise BundlerError(f"알 수 없는 종류: {', '.join(sorted(unknown))} (가능: {', '.join(KINDS)})")
        files, npm_files, pypi_files, images = (
            found if kind in kinds else [] for kind, found in zip(KINDS, (files, npm_files, pypi_files, images)))
        if not (files or npm_files or pypi_files or images):
            wanted = " / ".join(KINDS[k] for k in KINDS if k in kinds)
            raise BundlerError(f"이 폴더에는 올릴 {wanted} 이(가) 없습니다: {root}\n"
                               "오류: Step 1 에서 이 종류를 받은 폴더인지 확인하세요.")
    if not (files or npm_files or pypi_files or images):
        raise BundlerError(f"업로드할 파일이 없습니다: {root}")

    # 폴더에 들어 있는 종류마다 올릴 곳이 정해져 있어야 한다
    missing = [text for have, target, text in (
        (files, args.repo, "Maven / Gradle 라이브러리 → --repo (GUI: Maven repository)"),
        (npm_files, args.npm_repo, "npm 패키지 → --npm-repo (GUI: npm repository)"),
        (pypi_files, args.pypi_repo, "Python 패키지 → --pypi-repo (GUI: PyPI repository)"),
        (images, args.docker_url, "Docker 이미지 → --docker-url (GUI: Docker registry address)")) if have and not target]
    if missing:
        raise BundlerError("이 폴더에 들어 있는 것 중 올릴 곳이 지정되지 않은 것이 있습니다.\n"
                           + "\n".join(f"오류:   {m}" for m in missing))

    mib = lambda paths: sum(p.stat().st_size for p in paths) / 1024 / 1024   # noqa: E731
    docker_url = (args.docker_url or "").rstrip("/")
    if docker_url and "://" not in docker_url:
        docker_url = "http://" + docker_url
    base = f"{url}/repository/{args.repo}/"
    if files:
        print(f"업로드 대상: {len(files)}개 파일 ({mib([p for _, p in files]):.1f} MiB) → {base}")
    if npm_files:
        print(f"업로드 대상: npm 패키지 {len(npm_files)}개 ({mib(npm_files):.1f} MiB) → {url}/repository/{args.npm_repo}/")
    if pypi_files:
        print(f"업로드 대상: Python 패키지 파일 {len(pypi_files)}개 ({mib(pypi_files):.1f} MiB) → {url}/repository/{args.pypi_repo}/")
    if images:
        print(f"업로드 대상: Docker 이미지 {len(images)}개 → {docker_url}")
    if args.dry_run:
        print("--dry-run: 실제 업로드는 하지 않습니다.")
        return 0

    targets = {repo: fmt for have, repo, fmt in (
        (files, args.repo, "maven2"), (npm_files, args.npm_repo, "npm"), (pypi_files, args.pypi_repo, "pypi")) if have}
    if targets:
        preflight(client, url, targets)

    results: dict[str, list] = {"uploaded": [], "skipped": [], "failed": []}
    lock = threading.Lock()
    stop = threading.Event()
    done = [0]
    components = [(p.relative_to(root).as_posix(), p) for p in npm_files + pypi_files]
    total = len(files) + len(components)

    def work(item):
        rel, path = item
        if stop.is_set():
            return
        try:
            if rel.startswith("_npm/"):
                state, detail = post_component(client, url, args.npm_repo, "npm", path)
            elif rel.startswith("_pypi/"):
                state, detail = post_component(client, url, args.pypi_repo, "pypi", path)
            else:
                state, detail = put_file(client, base, rel, path, args.check_exists)
        except FatalUploadError as e:
            stop.set()
            raise e
        except BundlerError as e:
            state, detail = "failed", str(e)
        with lock:
            results[state].append({"path": rel, "detail": detail})
            done[0] += 1
            if state == "failed":
                log.error("실패: %s (%s)", rel, detail)
            if done[0] % 200 == 0:
                print(f"  진행 {done[0]}/{total}")

    phases: dict[int, list] = {}
    for item in files:
        phases.setdefault(priority(item[0]), []).append(item)
    if components:
        phases[9] = components
    fatal = None
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for prio in sorted(phases):
            futures = [pool.submit(work, item) for item in phases[prio]]
            for fut in futures:
                try:
                    fut.result()
                except FatalUploadError as e:
                    fatal = fatal or e
            if fatal:
                break

    pulls: list[str] = []
    if images and not fatal:
        try:
            registry = docker.Registry(no_proxy=args.no_proxy, user=args.user, password=password)
            pulls = docker.push(root, docker_url, registry, results)
        except BundlerError as e:
            fatal = FatalUploadError(f"Docker 이미지 업로드 실패: {e}")

    report_path = Path(args.report)
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"완료: 업로드 {len(results['uploaded'])}, 건너뜀 {len(results['skipped'])}, 실패 {len(results['failed'])} "
          f"(상세: {report_path})")
    for name in pulls:
        print(f"  docker pull {name}")
    if fatal:
        raise fatal
    return 1 if results["failed"] else 0
