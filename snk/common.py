"""공통 유틸리티: 설정 로딩, GAV, HTTP, 외부 명령 실행."""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import platform as _platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent
DEFAULT_REPOS_FILE = ROOT_DIR / "repos.json"

log = logging.getLogger("snk")

# 고정 버전: 플러그인 해석은 반드시 이 버전으로 수행해야 폐쇄망 검증과 수집 결과가 일치한다.
MAVEN_DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.8.1"


class BundlerError(Exception):
    """사용자에게 메시지로 보여줄 오류."""


# --------------------------------------------------------------------------- GAV

def version_key(version: str):
    parts = re.split(r"[.\-_+]", version)
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts)


@dataclass(frozen=True, order=True)
class GAV:
    group: str
    artifact: str
    version: str

    def __str__(self) -> str:
        return f"{self.group}:{self.artifact}:{self.version}"

    @property
    def rel_dir(self) -> str:
        return f"{self.group.replace('.', '/')}/{self.artifact}/{self.version}"

    def file(self, ext: str = "jar", classifier: str | None = None) -> str:
        name = f"{self.artifact}-{self.version}"
        if classifier:
            name += f"-{classifier}"
        return f"{self.rel_dir}/{name}.{ext}"

    @classmethod
    def parse(cls, text: str) -> "GAV":
        parts = text.strip().split(":")
        if len(parts) != 3 or not all(p.strip() for p in parts):
            raise BundlerError(f"좌표 형식이 올바르지 않습니다 (group:artifact:version 이어야 함): {text!r}")
        return cls(*[p.strip() for p in parts])


# ------------------------------------------------------------------------ 설정

def load_config(path: str | Path | None = None) -> dict:
    cfg_path = Path(path) if path else DEFAULT_REPOS_FILE
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise BundlerError(f"저장소 설정 파일을 찾을 수 없습니다: {cfg_path}")
    except json.JSONDecodeError as e:
        raise BundlerError(f"저장소 설정 파일 JSON 오류 ({cfg_path}): {e}")
    repos = cfg.get("repositories") or []
    if not repos:
        raise BundlerError(f"설정 파일에 repositories 가 비어 있습니다: {cfg_path}")
    for r in repos:
        if not r.get("id") or not r.get("url"):
            raise BundlerError(f"repositories 항목에는 id 와 url 이 필요합니다: {r}")
        r["url"] = r["url"].rstrip("/") + "/"
    cfg.setdefault("platforms", {})
    cfg.setdefault("native_hints", [])
    cfg.setdefault("default_platforms", list(cfg["platforms"])[:1])
    return cfg


def select_platforms(cfg: dict, names: str | list[str] | None) -> list[dict]:
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    names = names or cfg["default_platforms"]
    known = cfg["platforms"]
    result = []
    for n in names:
        if n not in known:
            raise BundlerError(f"알 수 없는 플랫폼 {n!r}. 사용 가능: {', '.join(known)}")
        result.append({"key": n, **known[n]})
    return result


def host_platform_key() -> str:
    os_name = {"linux": "linux", "win32": "windows", "darwin": "macos"}.get(sys.platform, sys.platform)
    machine = _platform.machine().lower()
    if machine in ("amd64", "x86_64"):
        arch = "x86_64"
    elif machine in ("arm64", "aarch64"):
        arch = "aarch64"
    else:
        arch = machine
    return f"{os_name}-{arch}"


# ----------------------------------------------------- 파일명 기반 플랫폼 필터

_ALL_OS_TOKENS = {"linux", "windows", "win", "win32", "win64", "macos", "osx", "mac", "darwin", "freebsd", "solaris"}
_ALL_ARCH_TOKENS = {"x86_64", "amd64", "x64", "aarch64", "aarch_64", "arm64", "x86", "i386", "i686",
                    "ppc64le", "ppc64", "s390x", "riscv64", "armv7", "arm"}


def file_matches_platforms(filename: str, platforms: list[dict]) -> bool:
    """파일명에 OS/CPU 토큰이 있으면 선택된 플랫폼에 해당할 때만 True. 토큰이 없으면 공통 파일로 본다."""
    stem = filename.lower().rsplit(".", 1)[0]
    tokens = set(re.split(r"[-.]", stem))
    os_found = tokens & _ALL_OS_TOKENS
    if os_found:
        wanted = {t for p in platforms for t in p.get("os_tokens", [])}
        if not (os_found & wanted):
            return False
    arch_found = tokens & _ALL_ARCH_TOKENS
    if arch_found:
        wanted = {t for p in platforms for t in p.get("arch_tokens", [])}
        if not (arch_found & wanted):
            return False
    return True


# --------------------------------------------------------------------- 파일 규칙

_JUNK_NAMES = {"_remote.repositories", "resolver-status.properties", ".DS_Store", "Thumbs.db"}
_JUNK_SUFFIXES = (".lastUpdated", ".part", ".tmp", ".sha1", ".md5", ".sha256", ".sha512")


def is_junk(name: str) -> bool:
    """Nexus 에 올리면 안 되거나 Nexus 가 스스로 만드는 파일."""
    return (name in _JUNK_NAMES or name.startswith("maven-metadata")
            or name.endswith(_JUNK_SUFFIXES))


def sha1_hex(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


# -------------------------------------------------------------------------- HTTP

class HttpClient:
    """urllib 기반 HTTP 클라이언트. 5xx/네트워크 오류는 재시도하고, 그 외 상태 코드는 그대로 돌려준다."""

    def __init__(self, *, no_proxy: bool = False, user: str | None = None, password: str | None = None,
                 timeout: float = 60, retries: int = 3):
        handlers = [urllib.request.ProxyHandler({})] if no_proxy else []
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout
        self.retries = retries
        self.auth_header = None
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode("utf-8")).decode("ascii")
            self.auth_header = f"Basic {token}"

    def request(self, method: str, url: str, *, data=None, headers: dict | None = None,
                body_factory=None) -> tuple[int, bytes]:
        """body_factory: 호출할 때마다 새 (본문, 길이) 를 돌려주는 함수(스트리밍 업로드 재시도용)."""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            hdrs = {"User-Agent": "sneakernet/0.1"}
            if self.auth_header:
                hdrs["Authorization"] = self.auth_header
            if headers:
                hdrs.update(headers)
            body, closer = data, None
            if body_factory is not None:
                body, length, closer = body_factory()
                hdrs["Content-Length"] = str(length)
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                with self.opener.open(req, timeout=self.timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as e:
                payload = e.read() if hasattr(e, "read") else b""
                if e.code >= 500 or e.code == 429:
                    last_exc = e
                else:
                    return e.code, payload
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last_exc = e
            finally:
                if closer:
                    closer()
            if attempt < self.retries:
                time.sleep(min(2 ** attempt, 8))
        raise BundlerError(f"요청 실패 {method} {url}: {last_exc}")

    def get(self, url: str) -> tuple[int, bytes]:
        return self.request("GET", url)

    def head(self, url: str) -> int:
        return self.request("HEAD", url)[0]


# ------------------------------------------------------------------- 외부 명령

def find_tool(name: str, override: str | None = None) -> str:
    cmd = override or name
    found = shutil.which(cmd)
    if not found:
        raise BundlerError(f"실행 파일 '{cmd}' 을(를) PATH 에서 찾을 수 없습니다. "
                           f"설치하거나 --{name}-cmd 로 경로를 지정하세요.")
    return found


def run_cmd(cmd: list[str], *, logfile: Path, cwd: Path | None = None, env: dict | None = None) -> int:
    logfile.parent.mkdir(parents=True, exist_ok=True)
    with open(logfile, "ab") as f:
        f.write(("$ " + " ".join(shlex.quote(str(c)) for c in cmd) + "\n").encode("utf-8"))
        f.flush()
        proc = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
    return proc.returncode


def log_tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])
