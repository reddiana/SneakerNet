"""Docker 이미지: 레지스트리 HTTP API(v2)로 직접 내려받고 올린다. docker 프로그램은 필요 없다.

반입 디렉토리의 _docker/ 구조:
  images.json            이미지 목록 (이름, 태그, 플랫폼별 매니페스트 digest)
  blobs/sha256/<hex>     매니페스트, 설정, 레이어 (digest 가 곧 파일 이름이라 이미지끼리 공유된다)
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin

from .common import BundlerError, log

INDEX_TYPES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json")
_ACCEPT = ", ".join(INDEX_TYPES + MANIFEST_TYPES)
_HUB = ("docker.io", "index.docker.io", "registry-1.docker.io")


@dataclass(frozen=True)
class ImageRef:
    text: str
    registry: str
    repo: str
    tag: str | None
    digest: str | None

    def __str__(self) -> str:
        return self.text

    @property
    def name(self) -> str:
        """Nexus 에 올릴 이름: 원래 레지스트리 주소는 빼고, Docker Hub 공식 이미지의 library/ 도 뺀다."""
        return self.repo[len("library/"):] if self.registry in _HUB and self.repo.startswith("library/") else self.repo

    @property
    def base(self) -> str:
        host = "registry-1.docker.io" if self.registry in _HUB else self.registry
        scheme = "http" if host.split(":")[0] in ("localhost", "127.0.0.1") else "https"
        return f"{scheme}://{host}/v2/{self.repo}"


def parse_ref(text: str) -> ImageRef:
    """'nginx:1.27', 'bitnami/redis:7.2', 'ghcr.io/org/app:1.0', 'nginx@sha256:...' 를 해석한다."""
    rest, _, digest = text.strip().partition("@")
    first, _, remainder = rest.partition("/")
    if remainder and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, remainder
    else:
        registry, path = "docker.io", rest
    tag = None
    if ":" in path.rsplit("/", 1)[-1]:
        path, tag = path.rsplit(":", 1)
    if (not path or re.search(r"[^a-z0-9._\-/]", path) or (tag is not None and not re.fullmatch(r"[\w][\w.\-]*", tag))
            or (digest and not re.fullmatch(r"[a-z0-9]+:[a-f0-9]{32,}", digest))):
        raise BundlerError(f"Docker 이미지 형식이 올바르지 않습니다 (이름:태그 이어야 함, 예: nginx:1.27): {text!r}")
    if registry in _HUB and "/" not in path:
        path = "library/" + path
    return ImageRef(text.strip(), registry, path, tag or (None if digest else "latest"), digest or None)


def wanted_platforms(platforms: list[dict]) -> list[str]:
    """선택된 플랫폼 중 컨테이너 이미지가 있는 것 ('linux/amd64' 등). 하나도 없으면 linux/amd64."""
    found = list(dict.fromkeys(p["docker_platform"] for p in platforms if p.get("docker_platform")))
    return found or ["linux/amd64"]


def _blob_path(root: Path, digest: str) -> Path:
    return root / "blobs" / digest.replace(":", "/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # 리다이렉트는 직접 따라간다 (인증 헤더를 넘기지 않으려고)
        return None


class Registry:
    """레지스트리 v2 클라이언트. Bearer 토큰 인증, 블롭 리다이렉트, 5xx/네트워크 오류 재시도를 처리한다."""

    def __init__(self, *, no_proxy: bool = False, user: str | None = None, password: str | None = None,
                 timeout: float = 120, retries: int = 3):
        handlers: list = [_NoRedirect()]
        if no_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.timeout = timeout
        self.retries = retries
        self.basic = None
        if user:
            self.basic = "Basic " + base64.b64encode(f"{user}:{password or ''}".encode("utf-8")).decode("ascii")
        self.auth = self.basic

    def _send(self, method: str, url: str, headers: dict, body_factory, auth: bool):
        hdrs = {"User-Agent": "sneakernet/0.1", **headers}
        if auth and self.auth:
            hdrs["Authorization"] = self.auth
        body = closer = None
        if body_factory is not None:
            body, length, closer = body_factory()
            hdrs["Content-Length"] = str(length)
        try:
            return self.opener.open(urllib.request.Request(url, data=body, method=method, headers=hdrs),
                                    timeout=self.timeout)
        except urllib.error.HTTPError as e:
            return e
        finally:
            if closer:
                closer()

    def _login(self, challenge: str) -> bool:
        """401 응답의 'Bearer realm=...' 안내에 따라 토큰을 받아 온다."""
        m = re.match(r"\s*Bearer\s+(.*)", challenge or "", re.I)
        params = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1))) if m else {}
        if not params.get("realm"):
            return False
        query = urlencode({k: params[k] for k in ("service", "scope") if k in params})
        saved, self.auth = self.auth, self.basic
        try:
            resp = self._send("GET", params["realm"] + ("?" + query if query else ""), {}, None, True)
            with resp:
                data = json.loads(resp.read().decode("utf-8")) if resp.code == 200 else {}
        except (OSError, ValueError):
            data = {}
        token = data.get("token") or data.get("access_token")
        self.auth = f"Bearer {token}" if token else saved
        return bool(token)

    def open(self, method: str, url: str, *, headers: dict | None = None, body_factory=None):
        """응답 객체를 돌려준다 (호출한 쪽에서 닫는다). 상태 코드는 .code, 헤더는 .headers."""
        headers = headers or {}
        last: object = None
        logged_in = False
        attempt = 0
        while attempt <= self.retries:
            try:
                resp = self._send(method, url, headers, body_factory, True)
                if resp.code == 401 and not logged_in and self._login(resp.headers.get("WWW-Authenticate", "")):
                    resp.close()
                    logged_in = True
                    continue
                if resp.code in (301, 302, 303, 307, 308) and method in ("GET", "HEAD") and resp.headers.get("Location"):
                    target = urljoin(url, resp.headers["Location"])
                    resp.close()
                    resp = self._send(method, target, {}, None, False)
                if resp.code < 500 and resp.code != 429:
                    return resp
                last = f"HTTP {resp.code}"
                resp.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
                last = e
            attempt += 1
            if attempt <= self.retries:
                time.sleep(min(2 ** attempt, 8))
        raise BundlerError(f"요청 실패 {method} {url}: {last}")

    def fetch(self, method: str, url: str, *, headers: dict | None = None, data: bytes | None = None):
        factory = (lambda: (data, len(data), None)) if data is not None else None
        with self.open(method, url, headers=headers, body_factory=factory) as resp:
            return resp.code, resp.headers, resp.read()

    def download(self, url: str, target: Path, digest: str) -> None:
        """블롭을 파일로 받으면서 digest 를 확인한다."""
        algo, _, expected = digest.partition(":")
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        with self.open("GET", url) as resp:
            if resp.code != 200:
                raise BundlerError(f"블롭을 받지 못했습니다 (HTTP {resp.code}): {url}")
            h = hashlib.new(algo)
            with open(part, "wb") as fh:
                while chunk := resp.read(1 << 20):
                    h.update(chunk)
                    fh.write(chunk)
        if h.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise BundlerError(f"블롭의 체크섬이 맞지 않습니다: {url}")
        part.replace(target)


# ------------------------------------------------------------------------- 수집

def _get_manifest(reg: Registry, ref: ImageRef, reference: str) -> tuple[bytes, str]:
    code, headers, body = reg.fetch("GET", f"{ref.base}/manifests/{reference}", headers={"Accept": _ACCEPT})
    if code in (401, 403, 404):
        raise BundlerError(f"이런 Docker 이미지는 없습니다 (또는 로그인이 필요한 비공개 이미지입니다): {ref}\n"
                           f"오류:   이름과 태그의 철자를 확인하세요. (HTTP {code}, {ref.base})")
    if code != 200:
        raise BundlerError(f"Docker 이미지 정보를 받지 못했습니다 (HTTP {code}): {ref}")
    try:
        media = json.loads(body).get("mediaType")
    except ValueError:
        raise BundlerError(f"Docker 이미지 정보를 읽을 수 없습니다: {ref}")
    return body, media or (headers.get("Content-Type") or "").split(";")[0].strip()


def check_refs(refs: list[ImageRef], no_proxy: bool) -> None:
    """직접 적은 이미지가 레지스트리에 있는지 먼저 확인한다 (없으면 오류)."""
    for ref in refs:
        _get_manifest(Registry(no_proxy=no_proxy, timeout=30, retries=1), ref, ref.digest or ref.tag)


def pull(ref: ImageRef, root: Path, wanted: list[str], no_proxy: bool, workers: int) -> dict:
    """이미지 하나를 root(_docker) 에 받고 images.json 에 넣을 기록을 돌려준다."""
    reg = Registry(no_proxy=no_proxy)
    body, media = _get_manifest(reg, ref, ref.digest or ref.tag)
    top = json.loads(body)
    record = {"source": str(ref), "name": ref.name, "tag": ref.tag, "index_media_type": None, "manifests": []}
    manifests: list[tuple[bytes, dict]] = []
    if media in INDEX_TYPES or "manifests" in top:
        record["index_media_type"] = media or INDEX_TYPES[0]
        available = []
        for entry in top.get("manifests", []):
            plat = entry.get("platform") or {}
            key = f"{plat.get('os')}/{plat.get('architecture')}"
            if key in wanted:
                data, _ = _get_manifest(reg, ref, entry["digest"])
                if hashlib.sha256(data).hexdigest() != entry["digest"].partition(":")[2]:
                    raise BundlerError(f"매니페스트의 체크섬이 맞지 않습니다: {ref} ({key})")
                manifests.append((data, {"digest": entry["digest"], "mediaType": entry.get("mediaType") or MANIFEST_TYPES[0],
                                         "platform": plat}))
            elif plat.get("os") not in (None, "unknown"):
                available.append(key)
        if not manifests:
            raise BundlerError(f"{ref}: 선택한 플랫폼({', '.join(wanted)})용 이미지가 없습니다. "
                               f"있는 플랫폼: {', '.join(dict.fromkeys(available)) or '없음'}")
    else:
        manifests.append((body, {"digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                                 "mediaType": media or MANIFEST_TYPES[1], "platform": None}))

    blobs: dict[str, None] = {}
    for data, entry in manifests:
        doc = json.loads(data)
        if "config" not in doc:
            raise BundlerError(f"{ref}: 지원하지 않는 옛 이미지 형식(schema 1)입니다.")
        path = _blob_path(root, entry["digest"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        blobs[doc["config"]["digest"]] = None
        for layer in doc.get("layers", []):
            if not layer.get("urls"):   # 다른 곳에서 받는 레이어(Windows 기본 레이어)는 레지스트리에 없다
                blobs[layer["digest"]] = None
        record["manifests"].append(entry)

    def fetch(digest: str) -> None:
        target = _blob_path(root, digest)
        if not target.is_file():
            reg.download(f"{ref.base}/blobs/{digest}", target, digest)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        list(pool.map(fetch, blobs))

    for data, entry in manifests:   # 단일 매니페스트는 플랫폼을 설정 블롭에서 읽는다
        if entry["platform"] is None:
            config = json.loads(_blob_path(root, json.loads(data)["config"]["digest"]).read_bytes())
            entry["platform"] = {"os": config.get("os"), "architecture": config.get("architecture")}
    return record


def collect(refs: list[ImageRef], out: Path, platforms: list[dict], no_proxy: bool, workers: int) -> tuple[int, list[str]]:
    """out/_docker 에 이미지를 모은다. (이미지 수, 경고) 를 돌려준다."""
    root = out / "_docker"
    root.mkdir(parents=True, exist_ok=True)
    wanted = wanted_platforms(platforms)
    warnings = []
    if not any(p.get("docker_platform") for p in platforms):
        warnings.append("Docker: 선택한 플랫폼에 컨테이너용이 없어 linux/amd64 이미지를 받았습니다.")
    records = []
    for ref in refs:
        log.info("Docker 이미지 받기: %s (%s)", ref, ", ".join(wanted))
        record = pull(ref, root, wanted, no_proxy, workers)
        for entry in record["manifests"]:
            key = f"{entry['platform'].get('os')}/{entry['platform'].get('architecture')}"
            if key not in wanted:
                warnings.append(f"Docker: {ref} 은(는) {key} 용 이미지 하나뿐이라 그대로 받았습니다.")
        records.append(record)
    (root / "images.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return len(records), warnings


# ------------------------------------------------------------------------ 업로드

def load_images(bundle: Path) -> list[dict]:
    try:
        return json.loads((bundle / "_docker" / "images.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _fail(code: int, what: str, body: bytes = b"") -> BundlerError:
    detail = body.decode("utf-8", errors="replace").strip().replace("\n", " ")[:200]
    if code in (401, 403):
        return BundlerError(f"HTTP {code}: Docker 레지스트리 인증/권한 오류입니다 ({what}). 계정과 저장소의 권한을 확인하세요.")
    if code == 404:
        return BundlerError(f"HTTP 404: Docker 레지스트리 주소가 올바르지 않습니다 ({what}). "
                            "docker (hosted) 저장소의 HTTP 커넥터 주소(예: http://nexus:8082)인지 확인하세요.")
    return BundlerError(f"HTTP {code} {detail} ({what})")


def _push_blob(reg: Registry, base: str, path: Path, digest: str) -> str:
    code, _, _ = reg.fetch("HEAD", f"{base}/blobs/{digest}")
    if code == 200:
        return "skipped"
    start = f"{base}/blobs/uploads/"
    code, headers, body = reg.fetch("POST", start, data=b"")
    if code not in (201, 202) or not headers.get("Location"):
        raise _fail(code, start, body)
    target = urljoin(start, headers["Location"])
    target += ("&" if "?" in target else "?") + "digest=" + quote(digest, safe="")
    size = path.stat().st_size

    def factory():
        fh = open(path, "rb")
        return fh, size, fh.close

    with reg.open("PUT", target, headers={"Content-Type": "application/octet-stream"}, body_factory=factory) as resp:
        if resp.code not in (201, 202, 204):
            raise _fail(resp.code, f"{base}/blobs/{digest}", resp.read())
    return "uploaded"


def _put_manifest(reg: Registry, base: str, reference: str, media: str, data: bytes) -> None:
    code, _, body = reg.fetch("PUT", f"{base}/manifests/{reference}", headers={"Content-Type": media}, data=data)
    if code not in (200, 201, 202):
        raise _fail(code, f"{base}/manifests/{reference}", body)


def push(bundle: Path, url: str, reg: Registry, results: dict) -> list[str]:
    """_docker 의 이미지를 레지스트리에 올린다. 올린 이미지를 받을 때 쓸 이름 목록을 돌려준다."""
    root = bundle / "_docker"
    host = url.split("://", 1)[-1]
    pushed = []
    for image in load_images(bundle):
        base = f"{url}/v2/{image['name']}"
        entries = []
        for entry in image["manifests"]:
            data = _blob_path(root, entry["digest"]).read_bytes()
            doc = json.loads(data)
            for desc in [doc["config"]] + doc.get("layers", []):
                blob = _blob_path(root, desc["digest"])
                if blob.is_file():
                    state = _push_blob(reg, base, blob, desc["digest"])
                    results[state].append({"path": f"_docker/{image['name']}/{desc['digest']}", "detail": ""})
            _put_manifest(reg, base, entry["digest"], entry["mediaType"], data)
            entries.append({"mediaType": entry["mediaType"], "size": len(data), "digest": entry["digest"],
                            "platform": entry["platform"]})
        if image.get("tag"):
            if len(entries) > 1:   # 받은 플랫폼만 담은 목록을 새로 만든다
                media = image.get("index_media_type") or INDEX_TYPES[0]
                index = json.dumps({"schemaVersion": 2, "mediaType": media, "manifests": entries}).encode("utf-8")
                _put_manifest(reg, base, image["tag"], media, index)
            else:
                _put_manifest(reg, base, image["tag"], entries[0]["mediaType"],
                              _blob_path(root, entries[0]["digest"]).read_bytes())
        name = f"{host}/{image['name']}" + (f":{image['tag']}" if image.get("tag") else f"@{image['manifests'][0]['digest']}")
        results["uploaded"].append({"path": f"_docker/{image['name']}:{image.get('tag')}", "detail": name})
        pushed.append(name)
    return pushed
