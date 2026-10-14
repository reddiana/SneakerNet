"""PyPI 수집: pip download 로 대상 플랫폼 / Python 버전용 wheel 을 의존성까지 내려받는다.

플랫폼을 지정하면 pip 은 wheel 만 받는다. 소스 배포본만 있는 패키지는 받지 못하고 오류로 알린다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

from .common import ROOT_DIR, BundlerError, HttpClient, log, log_tail, run_cmd, version_key

_SPEC_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)(?:\[[^\]]*\])?\s*(?:==\s*([^\s;,*]+))?$")
_FILE_RE = re.compile(r">\s*([^<>\s]+\.(?:whl|tar\.gz|zip))\s*<")
_NO_MATCH_RE = re.compile(r"No matching distribution found for (\S+)")


def host_python() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def parse_versions(text: str | None) -> list[str]:
    versions = [v.strip() for v in (text or host_python()).split(",") if v.strip()]
    for v in versions:
        if not re.fullmatch(r"\d+\.\d+", v):
            raise BundlerError(f"Python 버전 형식이 올바르지 않습니다 (예: 3.12 또는 3.11,3.12): {v!r}")
    return versions


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _trim(version: str) -> str:
    return re.sub(r"(\.0)+$", "", version.lower())


def index_versions(html: str) -> list[str]:
    """simple 색인 페이지의 파일 이름에서 버전 목록을 뽑는다."""
    versions = []
    for name in _FILE_RE.findall(html):
        stem = re.sub(r"\.(whl|tar\.gz|zip)$", "", name)
        parts = stem.split("-")
        version = parts[1] if name.endswith(".whl") and len(parts) > 1 else stem.rsplit("-", 1)[-1]
        if version not in versions:
            versions.append(version)
    return versions


def check_specs(specs: list[str], index: str, client: HttpClient) -> None:
    """직접 적은 패키지(이름 또는 이름==버전)가 색인에 있는지 먼저 확인하고, 없으면 바로 오류로 끝낸다."""
    missing = []
    for spec in specs:
        m = _SPEC_RE.match(spec.strip())
        if not m:
            continue   # 범위 지정 등은 pip 에 맡긴다
        name, version = m.group(1), m.group(2)
        try:
            code, body = client.get(f"{index}{_normalize(name)}/")
        except BundlerError:
            return   # 색인에 닿지 못하면 판단하지 않는다
        if code == 404:
            missing.append(f"오류: 이런 Python 패키지는 없습니다: {name}\n오류:   이름의 철자를 확인하세요.")
        elif code == 200 and version:
            versions = index_versions(body.decode("utf-8", errors="replace"))
            if versions and _trim(version) not in {_trim(v) for v in versions}:
                recent = sorted(versions, key=version_key)[-8:]
                missing.append(f"오류: 버전 {version} 은(는) 없습니다: {name}=={version}\n"
                               f"오류:   {name} 의 최근 버전: {', '.join(recent)}")
    if missing:
        raise BundlerError("PyPI 에 없는 패키지가 있어 다운로드를 시작하지 않았습니다.\n" + "\n".join(missing)
                           + f"\n오류: 찾아본 색인: {index}")


def marker_env(platform: dict, python_version: str) -> dict:
    """대상 플랫폼에서 pip 이 보게 될 환경 마커 값 (PEP 508)."""
    tokens = set(platform.get("os_tokens", []))
    arm = "aarch64" in platform["key"]
    if "windows" in tokens:
        system, sys_platform, os_name, machine = "Windows", "win32", "nt", "ARM64" if arm else "AMD64"
    elif "darwin" in tokens:
        system, sys_platform, os_name, machine = "Darwin", "darwin", "posix", "arm64" if arm else "x86_64"
    else:
        system, sys_platform, os_name, machine = "Linux", "linux", "posix", "aarch64" if arm else "x86_64"
    return {"implementation_name": "cpython", "implementation_version": python_version + ".0",
            "platform_python_implementation": "CPython", "python_version": python_version,
            "python_full_version": python_version + ".0", "platform_system": system, "sys_platform": sys_platform,
            "os_name": os_name, "platform_machine": machine}


def collect(inputs: list[Path], specs: list[str], out: Path, work: Path, index: str, platforms: list[dict],
            python_versions: list[str], no_proxy: bool) -> tuple[int, list[str], list[str]]:
    """out/_pypi 에 wheel 을 모은다. (파일 수, 경고, 문제) 를 돌려준다."""
    dest = out / "_pypi"
    dest.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    for idx, path in enumerate(inputs):
        sample = out / "_samples" / f"pypi-{idx:02d}"
        sample.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, sample / path.name)

    wanted: list[str] = []
    for path in inputs:
        wanted += ["-r", str(path)]
    wanted += specs
    targets = [p for p in platforms if p.get("pip_platforms")]
    for p in platforms:
        if p not in targets:
            warnings.append(f"PyPI: 플랫폼 {p['key']} 은(는) repos.json 에 pip_platforms 가 없어 건너뛰었습니다.")
    if not targets:
        raise BundlerError("PyPI: 선택한 플랫폼 중 pip 으로 받을 수 있는 것이 없습니다.")

    host = urlsplit(index)
    for plat in targets:
        for pyver in python_versions:
            cmd = [sys.executable, "-m", "snk.pip_target", "download", "--dest", str(dest), "--only-binary=:all:",
                   "--implementation", "cp", "--python-version", pyver, "--index-url", index,
                   "--disable-pip-version-check", "--no-input", "--progress-bar", "off"]
            for tag in plat["pip_platforms"]:
                cmd += ["--platform", tag]
            if host.scheme == "http":
                cmd += ["--trusted-host", host.hostname or ""]
            env = dict(os.environ, SNK_MARKER_ENV=json.dumps(marker_env(plat, pyver)), PYTHONUTF8="1")
            if no_proxy:
                env.update(NO_PROXY="*", no_proxy="*")
            logfile = work / "logs" / f"pip-{plat['key']}-py{pyver}.log"
            log.info("pip download: %s, Python %s", plat["key"], pyver)
            if run_cmd(cmd + wanted, logfile=logfile, cwd=ROOT_DIR, env=env) != 0:
                raise _explain(logfile, plat["key"], pyver)
    count = sum(1 for f in dest.iterdir() if f.is_file())
    return count, warnings, []


def _explain(logfile: Path, platform: str, python_version: str) -> BundlerError:
    try:
        text = logfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    names = list(dict.fromkeys(_NO_MATCH_RE.findall(text)))
    if not names:
        return BundlerError(f"pip download 가 실패했습니다 (대상: {platform}, Python {python_version}, 로그: {logfile})\n"
                            f"{log_tail(logfile, 15)}")
    lines = [f"오류: 받을 수 없습니다: {n}  (대상: {platform}, Python {python_version})" for n in names]
    return BundlerError("PyPI 에서 받지 못한 패키지가 있습니다.\n" + "\n".join(lines)
                        + "\n오류:   이름이나 버전이 틀렸거나, 이 운영체제 / Python 버전용 wheel 이 없는 패키지입니다."
                          "\n오류:   (소스 배포본만 있는 패키지는 받지 않습니다. Python versions 가 폐쇄망의 Python 과 같은지도 확인하세요.)"
                        + f"\n오류: 전체 로그: {logfile}")
