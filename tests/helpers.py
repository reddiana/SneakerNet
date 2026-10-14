"""테스트 보조: 가짜 Maven 저장소 생성, 가짜 Nexus/저장소 HTTP 서버, 가짜 Docker 레지스트리, 가짜 mvn."""
from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

POM_NS = 'xmlns="http://maven.apache.org/POM/4.0.0"'


def jar_bytes(label: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        z.writestr("label.txt", label)
    return buf.getvalue()


def write(root: Path, rel: str, data: bytes | str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    (root / (rel + ".sha1")).write_text(hashlib.sha1(data).hexdigest(), encoding="ascii")


def pom(group, artifact, version, *, parent=None, packaging=None, deps=(), props=None, imports=()) -> str:
    parent_xml = ""
    if parent:
        parent_xml = (f"<parent><groupId>{parent[0]}</groupId><artifactId>{parent[1]}</artifactId>"
                      f"<version>{parent[2]}</version></parent>")
    props_xml = "".join(f"<{k}>{v}</{k}>" for k, v in (props or {}).items())
    deps_xml = "".join(f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version></dependency>"
                       for g, a, v in deps)
    imports_xml = "".join(
        f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>"
        f"<type>pom</type><scope>import</scope></dependency>" for g, a, v in imports)
    dm = f"<dependencyManagement><dependencies>{imports_xml}</dependencies></dependencyManagement>" if imports else ""
    return (f'<?xml version="1.0"?><project {POM_NS}><modelVersion>4.0.0</modelVersion>{parent_xml}'
            f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"
            f"{'<packaging>' + packaging + '</packaging>' if packaging else ''}"
            f"<properties>{props_xml}</properties>{dm}<dependencies>{deps_xml}</dependencies></project>")


def add_artifact(root: Path, g, a, v, *, jar=True, **pom_kwargs) -> None:
    base = f"{g.replace('.', '/')}/{a}/{v}/{a}-{v}"
    write(root, base + ".pom", pom(g, a, v, **pom_kwargs))
    if jar:
        write(root, base + ".jar", jar_bytes(f"{g}:{a}:{v}"))


def build_fake_repo(root: Path) -> Path:
    """lib-a → lib-b:1.0, lib-c → lib-b:2.0 (버전 충돌), parent/BOM, .module, 네이티브 classifier 를 가진 저장소."""
    root.mkdir(parents=True, exist_ok=True)
    add_artifact(root, "com.example", "parent", "1.0", jar=False, packaging="pom", props={"bom.version": "1.0"})
    add_artifact(root, "com.example", "bom", "1.0", jar=False, packaging="pom")
    add_artifact(root, "com.example", "lib-b", "1.0", parent=("com.example", "parent", "1.0"))
    add_artifact(root, "com.example", "lib-b", "2.0", parent=("com.example", "parent", "1.0"))
    add_artifact(root, "com.example", "lib-a", "1.0", parent=("com.example", "parent", "1.0"),
                 deps=[("com.example", "lib-b", "1.0")], imports=[("com.example", "bom", "${bom.version}")])
    add_artifact(root, "com.example", "lib-c", "1.0", deps=[("com.example", "lib-b", "2.0")])

    b2 = "com/example/lib-b/2.0/lib-b-2.0"
    write(root, b2 + ".module", json.dumps({
        "formatVersion": "1.1",
        "variants": [
            {"name": "apiElements", "files": [{"name": "lib-b-2.0.jar", "url": "lib-b-2.0.jar"}]},
            {"name": "linuxNatives", "files": [{"name": "lib-b-2.0-natives-linux.jar", "url": "lib-b-2.0-natives-linux.jar"}]},
            {"name": "macNatives", "files": [{"name": "lib-b-2.0-natives-macos.jar", "url": "lib-b-2.0-natives-macos.jar"}]},
            {"name": "sourcesElements", "attributes": {"org.gradle.category": "documentation"},
             "files": [{"name": "lib-b-2.0-sources.jar", "url": "lib-b-2.0-sources.jar"}]},
        ]}))
    for suffix in ("-natives-linux", "-natives-macos", "-sources"):
        write(root, f"{b2}{suffix}.jar", jar_bytes(suffix))

    add_artifact(root, "io.netty", "netty-tcnative-boringssl-static", "2.0.0")
    for cls in ("linux-x86_64", "windows-x86_64", "osx-x86_64"):
        write(root, f"io/netty/netty-tcnative-boringssl-static/2.0.0/netty-tcnative-boringssl-static-2.0.0-{cls}.jar",
              jar_bytes(cls))
    return root


class FakeServer:
    """로컬 디렉토리를 Maven 저장소 겸 (선택적으로) Nexus 처럼 서빙하는 HTTP 서버."""

    def __init__(self, root: Path, *, nexus_repo: str | None = None, allow_redeploy: bool = True,
                 auth: tuple[str, str] | None = None, other_repos: dict[str, str] | None = None):
        self.root = root
        self.nexus_repo = nexus_repo
        self.other_repos = other_repos or {}          # {저장소 이름: 형식} (npm, pypi)
        self.components: list[tuple[str, str, str]] = []   # components API 로 받은 (저장소, 필드, 파일 이름)
        self.allow_redeploy = allow_redeploy
        self.auth = auth
        self.put_log: list[str] = []
        self.requests: list[tuple[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 조용히
                pass

            def _check_auth(self) -> bool:
                if not outer.auth:
                    return True
                import base64
                expected = "Basic " + base64.b64encode(f"{outer.auth[0]}:{outer.auth[1]}".encode()).decode()
                if self.headers.get("Authorization") == expected:
                    return True
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fake"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False

            def _file_for(self) -> Path | None:
                path = self.path.split("?")[0]
                if outer.nexus_repo:
                    prefix = f"/repository/{outer.nexus_repo}/"
                    if not path.startswith(prefix):
                        return None
                    path = "/" + path[len(prefix):]
                return outer.root / path.lstrip("/")

            def _send(self, status: int, body: bytes = b"", ctype="application/octet-stream", head=False):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if not head:
                    self.wfile.write(body)

            def do_GET(self, head=False):
                outer.requests.append((self.command, self.path))
                if not self._check_auth():
                    return
                if self.path.startswith("/service/rest/v1/status"):
                    return self._send(200, b"", head=head)
                if self.path.startswith("/service/rest/v1/repositories"):
                    repos = [{"name": outer.nexus_repo or "none", "format": "maven2", "type": "hosted"}]
                    repos += [{"name": n, "format": f, "type": "hosted"} for n, f in outer.other_repos.items()]
                    body = json.dumps(repos).encode()
                    return self._send(200, body, "application/json", head)
                f = self._file_for()
                if f is not None and f.is_file():
                    return self._send(200, f.read_bytes(), head=head)
                if f is not None and (f / "index.html").is_file():   # PyPI simple 색인
                    return self._send(200, (f / "index.html").read_bytes(), "text/html", head)
                self._send(404, b"not found", head=head)

            def do_POST(self):
                outer.requests.append(("POST", self.path))
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if not self._check_auth():
                    return
                m = re.match(r"/service/rest/v1/components\?repository=([^&]+)$", self.path)
                part = re.search(rb'name="([^"]+)"; filename="([^"]+)"', data)
                if not m or m.group(1) not in outer.other_repos or not part:
                    return self._send(404, b"no repo")
                item = (m.group(1), part.group(1).decode(), part.group(2).decode())
                if item in outer.components and not outer.allow_redeploy:
                    return self._send(400, b"Repository does not allow updating assets: " + m.group(1).encode())
                outer.components.append(item)
                self._send(204)

            def do_HEAD(self):
                self.do_GET(head=True)

            def do_PUT(self):
                outer.requests.append(("PUT", self.path))
                length = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(length)
                if not self._check_auth():
                    return
                f = self._file_for()
                if f is None:
                    return self._send(404, b"no repo")
                if f.exists() and not outer.allow_redeploy:
                    return self._send(400, b"Repository does not allow updating assets: " + (outer.nexus_repo or "").encode())
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
                outer.put_log.append(self.path)
                self._send(201)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def npm_tarball(name: str, version: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = json.dumps({"name": name, "version": version}).encode()
        info = tarfile.TarInfo("package/package.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def wheel_bytes(name: str, version: str, requires=()) -> bytes:
    info = f"{name}-{version}.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {r}\n" for r in requires)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{name}/__init__.py", "")
        z.writestr(f"{info}/METADATA", meta)
        z.writestr(f"{info}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        z.writestr(f"{info}/RECORD", "")
    return buf.getvalue()


def build_fake_pypi(root: Path, wheels: dict[str, tuple[str, tuple]]) -> None:
    """root/simple/<이름>/ 에 색인과 wheel 을 만든다. wheels: {이름: (버전, 의존성들)}."""
    for name, (version, requires) in wheels.items():
        d = root / "simple" / name
        d.mkdir(parents=True, exist_ok=True)
        filename = f"{name}-{version}-py3-none-any.whl"
        (d / filename).write_bytes(wheel_bytes(name, version, requires))
        (d / "index.html").write_text(f'<html><body><a href="{filename}">{filename}</a></body></html>', encoding="utf-8")


class FakeRegistry:
    """메모리에 이미지를 담는 Docker 레지스트리 (v2 API 중 받기/올리기에 쓰는 부분만)."""

    def __init__(self, auth: tuple[str, str] | None = None):
        self.blobs: dict[str, bytes] = {}
        self.manifests: dict[tuple[str, str], tuple[str, bytes]] = {}
        self.auth = auth
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status: int, body: bytes = b"", headers: dict | None = None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _route(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if outer.auth:
                    import base64
                    if self.headers.get("Authorization") != "Basic " + base64.b64encode(":".join(outer.auth).encode()).decode():
                        return self._send(401, b"", {"WWW-Authenticate": 'Basic realm="fake"'})
                path, _, query = self.path.partition("?")
                m = re.fullmatch(r"/v2/(.+)/(manifests|blobs)/(.+)", path)
                if not m:
                    return self._send(404)
                name, kind, ref = m.groups()
                if self.command in ("GET", "HEAD"):
                    if kind == "manifests" and (name, ref) in outer.manifests:
                        media, body = outer.manifests[(name, ref)]
                        return self._send(200, body, {"Content-Type": media})
                    if kind == "blobs" and ref in outer.blobs:
                        return self._send(200, outer.blobs[ref])
                    return self._send(404)
                if self.command == "POST" and kind == "blobs" and ref == "uploads/":
                    return self._send(202, b"", {"Location": f"/v2/{name}/blobs/uploads/{uuid.uuid4().hex}"})
                if self.command == "PUT" and kind == "blobs":
                    digest = dict(p.split("=", 1) for p in query.split("&"))["digest"].replace("%3A", ":")
                    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
                        return self._send(400, b"digest mismatch")
                    outer.blobs[digest] = data
                    return self._send(201)
                if self.command == "PUT" and kind == "manifests":
                    doc = json.loads(data)
                    needed = [d["digest"] for d in [doc.get("config", {})] + doc.get("layers", []) if d.get("digest")]
                    if any(d not in outer.blobs for d in needed):
                        return self._send(400, b"blob unknown")
                    if any((name, e["digest"]) not in outer.manifests for e in doc.get("manifests", [])):
                        return self._send(400, b"manifest unknown")
                    media = self.headers.get("Content-Type")
                    outer.manifests[(name, ref)] = (media, data)
                    outer.manifests[(name, "sha256:" + hashlib.sha256(data).hexdigest())] = (media, data)
                    return self._send(201)
                self._send(405)

            do_GET = do_HEAD = do_POST = do_PUT = _route

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def add_image(self, name: str, tag: str, archs=("amd64", "arm64")) -> dict[str, str]:
        """플랫폼별 이미지와 그 목록(index)을 넣는다. {arch: 매니페스트 digest} 를 돌려준다."""
        def blob(data: bytes) -> dict:
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            self.blobs[digest] = data
            return {"digest": digest, "size": len(data)}

        digests, entries = {}, []
        media = "application/vnd.oci.image.manifest.v1+json"
        for arch in archs:
            config = json.dumps({"os": "linux", "architecture": arch}).encode()
            manifest = json.dumps({
                "schemaVersion": 2, "mediaType": media,
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json", **blob(config)},
                "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", **blob(f"layer-{arch}".encode())},
                           {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", **blob(b"shared-layer")}],
            }).encode()
            digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
            self.manifests[(name, digest)] = (media, manifest)
            digests[arch] = digest
            entries.append({"mediaType": media, "digest": digest, "size": len(manifest),
                            "platform": {"os": "linux", "architecture": arch}})
        index_media = "application/vnd.oci.image.index.v1+json"
        index = json.dumps({"schemaVersion": 2, "mediaType": index_media, "manifests": entries}).encode()
        self.manifests[(name, tag)] = (index_media, index)
        return digests

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def write_repos_config(path: Path, repo_url: str) -> Path:
    cfg = json.loads((Path(__file__).resolve().parent.parent / "repos.json").read_text(encoding="utf-8"))
    cfg["repositories"] = [{"id": "central", "url": repo_url + "/"}]
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


FAKE_MVN = r'''#!/usr/bin/env python3
"""테스트용 가짜 mvn: 명령 구성을 검사하고, go-offline/tree 동작을 흉내 낸다."""
import os, re, shutil, sys
from pathlib import Path

args = sys.argv[1:]
repo_src = Path(os.environ["FAKE_REPO_DIR"])
log = Path(os.environ["FAKE_MVN_LOG"])
with open(log, "a") as f:
    f.write(" ".join(args) + "\n")

m2 = next((Path(a.split("=", 1)[1]) for a in args if a.startswith("-Dmaven.repo.local=")), None)
pom = Path(args[args.index("-f") + 1])
goal = args[-1] if not args[-1].startswith("-") else next(a for a in args if ":" in a and not a.startswith("-"))
for a in args:
    if a.endswith(":go-offline") or a.endswith(":tree"):
        goal = a
assert "-s" in args and "-gs" in args and m2 is not None

def deps_of(pom_path):
    text = Path(pom_path).read_text()
    return re.findall(r"<dependency><groupId>([^<]+)</groupId><artifactId>([^<]+)</artifactId><version>([^<]+)</version>", text)

def rel(g, a, v, ext): return f"{g.replace('.', '/')}/{a}/{v}/{a}-{v}.{ext}"

def copy(g, a, v, seen):
    if (g, a, v) in seen: return
    seen.add((g, a, v))
    for ext in ("pom", "jar"):
        src = repo_src / rel(g, a, v, ext)
        if src.exists():
            dst = m2 / rel(g, a, v, ext)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            (dst.parent / "_remote.repositories").write_text("x")
            (dst.parent / (dst.name + ".lastUpdated")).write_text("x")
    pom_src = repo_src / rel(g, a, v, "pom")
    if pom_src.exists():
        text = pom_src.read_text()
        m = re.search(r"<parent><groupId>([^<]+)</groupId><artifactId>([^<]+)</artifactId><version>([^<]+)</version>", text)
        if m: copy(*m.groups(), seen)
        for d in deps_of(pom_src): copy(*d, seen)

if goal.endswith(":go-offline"):
    poms = [pom]
    text = pom.read_text()
    for mod in re.findall(r"<module>([^<]+)</module>", text):
        poms.append(pom.parent / mod / "pom.xml")
    seen = set()
    for p in poms:
        for d in deps_of(p): copy(*d, seen)
    (m2 / "org/apache/maven/plugins").mkdir(parents=True, exist_ok=True)
    sys.exit(0)

if goal.endswith(":tree"):
    out = Path(next(a.split("=", 1)[1] for a in args if a.startswith("-DoutputFile=")))
    lines = ["bundler.tmp:input:jar:1"]
    deps = deps_of(pom)
    for i, (g, a, v) in enumerate(deps):
        lines.append(("\\- " if i == len(deps) - 1 else "+- ") + f"{g}:{a}:jar:{v}:compile")
    extra = os.environ.get("FAKE_TREE_EXTRA")
    if extra: lines.append(extra)
    out.write_text("\n".join(lines) + "\n")
    sys.exit(0)

sys.exit(0)
'''
