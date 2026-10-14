"""npm 수집: package-lock.json 에 적힌 tarball 을 그대로 내려받는다.

lock 파일이 없으면(package.json 만 있거나 이름@버전만 받은 경우) npm 으로 lock 파일만 먼저 만든다. 설치는 하지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlsplit

from .common import BundlerError, HttpClient, find_tool, log, log_tail, run_cmd, version_key

_SPEC_RE = re.compile(r"^(@[^/@\s]+/[^/@\s]+|[^/@\s]+)(?:@(\S+))?$")
_EXACT_RE = re.compile(r"^\d+\.\d+\.\d+[\w.+\-]*$")
_LOCK_NAMES = ("package-lock.json", "npm-shrinkwrap.json")


def parse_spec(text: str) -> tuple[str, str]:
    """'express@4.19.2', '@types/node@20.1.0', 'express' → (이름, 버전 또는 'latest')."""
    m = _SPEC_RE.match(text.strip())
    if not m:
        raise BundlerError(f"npm 패키지 형식이 올바르지 않습니다 (이름@버전 이어야 함): {text!r}")
    return m.group(1), m.group(2) or "latest"


def check_specs(specs: list[tuple[str, str]], registry: str, client: HttpClient) -> None:
    """직접 적은 패키지가 저장소에 있는지 먼저 확인하고, 없으면 바로 오류로 끝낸다."""
    missing = []
    for name, version in specs:
        try:
            code, body = client.request("GET", registry + quote(name, safe="@"),
                                        headers={"Accept": "application/vnd.npm.install-v1+json"})
        except BundlerError:
            return   # 저장소에 닿지 못하면 판단하지 않는다
        if code == 404:
            missing.append(f"오류: 이런 npm 패키지는 없습니다: {name}\n오류:   이름의 철자를 확인하세요.")
        elif code == 200 and _EXACT_RE.match(version):
            versions = list(json.loads(body.decode("utf-8")).get("versions") or {})
            if version not in versions:
                recent = sorted(versions, key=version_key)[-8:]
                missing.append(f"오류: 버전 {version} 은(는) 없습니다: {name}@{version}\n"
                               f"오류:   {name} 의 최근 버전: {', '.join(recent)}")
    if missing:
        raise BundlerError("npm 저장소에 없는 패키지가 있어 다운로드를 시작하지 않았습니다.\n" + "\n".join(missing)
                           + f"\n오류: 찾아본 저장소: {registry}")


_NETWORK_RE = re.compile(r"ETIMEDOUT|ECONNREFUSED|ECONNRESET|EAI_AGAIN|ENOTFOUND")


def make_lock(project: Path, npm: str, registry: str, logfile: Path, no_proxy: bool, warnings: list[str]) -> Path:
    cmd = [npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund", "--registry", registry]
    direct = ["--no-proxy", "--no-https-proxy"]
    rc = run_cmd(cmd + (direct if no_proxy else []), logfile=logfile, cwd=project)
    if rc != 0 and not no_proxy and _NETWORK_RE.search(log_tail(logfile, 40)):
        # npm 설정(.npmrc)의 프록시가 지금 네트워크에서 닿지 않는 경우가 있어, 프록시 없이 한 번 더 해 본다.
        rc = run_cmd(cmd + direct, logfile=logfile, cwd=project)
        if rc == 0:
            warnings.append("npm 설정의 프록시로는 저장소에 연결되지 않아, 프록시 없이 다시 실행했습니다.")
    lock = project / "package-lock.json"
    if rc != 0 or not lock.is_file():
        raise BundlerError(f"npm 이 의존성을 해석하지 못했습니다 (로그: {logfile})\n{log_tail(logfile, 15)}")
    return lock


def lock_packages(lock: dict) -> tuple[list[dict], list[str]]:
    """lock 파일에서 내려받을 패키지 목록과, 저장소가 아닌 곳(git, 로컬 경로)을 가리켜 받지 못하는 항목 이름."""
    found, skipped = [], []

    def add(name: str, entry: dict) -> None:
        resolved = str(entry.get("resolved") or "")
        if resolved.startswith(("http://", "https://")):
            found.append({**entry, "name": name})
        elif resolved and not entry.get("link"):
            skipped.append(f"{name} ({resolved[:60]})")

    if isinstance(lock.get("packages"), dict):   # lockfileVersion 2, 3
        for key, entry in lock["packages"].items():
            if key:
                add(entry.get("name") or key.rsplit("node_modules/", 1)[-1], entry)
    else:                                        # lockfileVersion 1
        def walk(deps: dict | None) -> None:
            for name, entry in (deps or {}).items():
                add(name, entry)
                walk(entry.get("dependencies"))
        walk(lock.get("dependencies"))
    return found, skipped


def _match(values, tokens: set[str]) -> bool:
    if not values:
        return True
    values = [values] if isinstance(values, str) else values
    deny = {v[1:] for v in values if v.startswith("!")}
    allow = {v for v in values if not v.startswith("!")}
    return not (deny & tokens) and (not allow or bool(allow & tokens))


def wanted(entry: dict, platforms: list[dict]) -> bool:
    """패키지의 os / cpu 제한이 선택된 플랫폼 중 하나에 맞는가 (제한이 없으면 공통 패키지)."""
    return any(_match(entry.get("os"), set(p.get("os_tokens", []))) and _match(entry.get("cpu"), set(p.get("arch_tokens", [])))
               for p in platforms)


def _intact(data: bytes, integrity: str | None) -> bool:
    for item in (integrity or "").split():
        algo, _, digest = item.partition("-")
        if algo in ("sha512", "sha384", "sha256", "sha1"):
            return base64.b64encode(hashlib.new(algo, data).digest()).decode("ascii") == digest.split("?")[0]
    return True


def collect(inputs: list[Path], specs: list[tuple[str, str]], out: Path, work: Path, registry: str,
            platforms: list[dict], npm_cmd: str | None, client: HttpClient, workers: int,
            no_proxy: bool = False) -> tuple[int, list[str], list[str]]:
    """out/_npm 에 tarball 을 모은다. (파일 수, 경고, 문제) 를 돌려준다."""
    dest = out / "_npm"
    warnings: list[str] = []
    problems: list[str] = []
    locks: list[tuple[str, Path]] = []
    projects = [(p.name, p) for p in inputs]
    if specs:
        synthetic = work / "npm" / "packages" / "package.json"
        synthetic.parent.mkdir(parents=True, exist_ok=True)
        synthetic.write_text(json.dumps({"name": "sneakernet-packages", "version": "1.0.0", "private": True,
                                         "dependencies": dict(specs)}, indent=2), encoding="utf-8")
        projects.append(("packages", synthetic))

    for idx, (label, path) in enumerate(projects):
        try:
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
        except ValueError as e:
            raise BundlerError(f"JSON 파일을 읽을 수 없습니다: {path} ({e})")
        sample = out / "_samples" / f"npm-{idx:02d}"
        sample.mkdir(parents=True, exist_ok=True)
        if "lockfileVersion" in doc:
            lock = path
        else:
            lock = next((path.parent / n for n in _LOCK_NAMES if (path.parent / n).is_file()), None)
            shutil.copy2(path, sample / "package.json")
            if lock is None or path.parent == work / "npm" / "packages":
                proj = work / "npm" / str(idx)
                proj.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, proj / "package.json")
                log.info("npm 해석: %s", label)
                lock = make_lock(proj, find_tool("npm", npm_cmd), registry, work / "logs" / f"npm-{idx}.log",
                                 no_proxy, warnings)
        shutil.copy2(lock, sample / "package-lock.json")
        locks.append((label, lock))

    jobs: dict[str, dict] = {}
    for label, lock in locks:
        packages, skipped = lock_packages(json.loads(lock.read_text(encoding="utf-8-sig")))
        if skipped:
            warnings.append(f"{label}: 저장소가 아닌 곳(git, 파일 경로)을 가리키는 npm 패키지 {len(skipped)}개는 받지 않았습니다: "
                            + ", ".join(skipped[:5]))
        for entry in packages:
            if wanted(entry, platforms):
                jobs.setdefault(entry["resolved"], entry)
    if not jobs:
        warnings.append("npm: 내려받을 패키지가 없습니다 (의존성이 없는 package.json 입니다).")
        return 0, warnings, problems

    def fetch(entry: dict) -> str | None:
        url = entry["resolved"]
        target = dest / entry["name"] / Path(urlsplit(url).path).name
        try:
            code, body = client.get(url)
        except BundlerError as e:
            return f"npm {entry['name']}@{entry.get('version')}: {e}"
        if code != 200:
            return f"npm {entry['name']}@{entry.get('version')}: HTTP {code} ({url})"
        if not _intact(body, entry.get("integrity")):
            return f"npm {entry['name']}@{entry.get('version')}: 체크섬이 맞지 않습니다 ({url})"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return None

    log.info("npm 다운로드: 패키지 %d개", len(jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        problems += [p for p in pool.map(fetch, jobs.values()) if p]
    return len(jobs) - len(problems), warnings, problems
