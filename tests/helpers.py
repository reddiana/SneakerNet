"""테스트 보조: 가짜 Maven 저장소 생성, 가짜 Nexus/저장소 HTTP 서버, 가짜 mvn."""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
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
                 auth: tuple[str, str] | None = None):
        self.root = root
        self.nexus_repo = nexus_repo
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
                    body = json.dumps([{"name": outer.nexus_repo or "none", "format": "maven2", "type": "hosted"}]).encode()
                    return self._send(200, body, "application/json", head)
                f = self._file_for()
                if f is not None and f.is_file():
                    return self._send(200, f.read_bytes(), head=head)
                self._send(404, b"not found", head=head)

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
