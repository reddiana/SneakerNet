"""upload 서브커맨드: 반입 디렉토리를 Nexus Repository 3 의 maven2 hosted 저장소에 HTTP PUT 으로 올린다.

표준 라이브러리만 사용하므로 폐쇄망 PC 에 별도 설치가 필요 없다.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

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


def preflight(client: HttpClient, url: str, repo: str) -> None:
    status, _ = client.get(f"{url}/service/rest/v1/status")
    if status != 200:
        log.warning("Nexus 상태 확인 실패 (HTTP %s). 주소가 올바른지 확인하세요: %s", status, url)
    status, body = client.get(f"{url}/service/rest/v1/repositories")
    if status == 200:
        repos = json.loads(body.decode("utf-8"))
        match = next((r for r in repos if r.get("name") == repo), None)
        if match is None:
            raise BundlerError(f"Nexus 에 저장소 {repo!r} 가 없습니다. 있는 저장소: {', '.join(r.get('name', '?') for r in repos)}")
        if match.get("format") != "maven2" or match.get("type") != "hosted":
            raise BundlerError(f"저장소 {repo!r} 는 maven2 hosted 가 아닙니다 (format={match.get('format')}, type={match.get('type')}).")
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
    base = f"{url}/repository/{args.repo}/"
    client = HttpClient(no_proxy=args.no_proxy, user=args.user, password=password)

    files = []
    for rel, path in list_files(root):
        if "-SNAPSHOT/" in rel:
            log.warning("SNAPSHOT 은 올리지 않습니다: %s", rel)
            continue
        files.append((rel, path))
    if not files:
        raise BundlerError(f"업로드할 파일이 없습니다: {root}")
    total_mib = sum(p.stat().st_size for _, p in files) / 1024 / 1024
    print(f"업로드 대상: {len(files)}개 파일 ({total_mib:.1f} MiB) → {base}")
    if args.dry_run:
        print("--dry-run: 실제 업로드는 하지 않습니다.")
        return 0

    preflight(client, url, args.repo)

    results: dict[str, list] = {"uploaded": [], "skipped": [], "failed": []}
    lock = threading.Lock()
    stop = threading.Event()
    done = [0]

    def work(item):
        rel, path = item
        if stop.is_set():
            return
        try:
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
                print(f"  진행 {done[0]}/{len(files)}")

    phases: dict[int, list] = {}
    for item in files:
        phases.setdefault(priority(item[0]), []).append(item)
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

    report_path = Path(args.report)
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"완료: 업로드 {len(results['uploaded'])}, 건너뜀 {len(results['skipped'])}, 실패 {len(results['failed'])} "
          f"(상세: {report_path})")
    if fatal:
        raise fatal
    return 1 if results["failed"] else 0
