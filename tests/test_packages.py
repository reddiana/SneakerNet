"""npm / PyPI / Docker 수집과 업로드 (가짜 저장소, 가짜 레지스트리, 가짜 Nexus)."""
import base64
import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import sneakernet  # noqa: E402
from helpers import FakeRegistry, FakeServer, check_docker_archive, build_fake_pypi, npm_tarball, write_repos_config  # noqa: E402
from snk import docker, npm, pypi  # noqa: E402
from snk.common import BundlerError, load_config, select_platforms  # noqa: E402


def run_cli(*argv) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = sneakernet.main([str(a) for a in argv])
    return rc, out.getvalue() + err.getvalue()


class ParseTests(unittest.TestCase):
    def test_npm_spec(self):
        self.assertEqual(npm.parse_spec("express@4.19.2"), ("express", "4.19.2"))
        self.assertEqual(npm.parse_spec("@types/node@20.1.0"), ("@types/node", "20.1.0"))
        self.assertEqual(npm.parse_spec("@types/node"), ("@types/node", "latest"))
        with self.assertRaises(BundlerError):
            npm.parse_spec("a b")

    def test_npm_platform_filter(self):
        platforms = select_platforms(load_config(), "linux-x86_64,windows-x86_64")
        self.assertTrue(npm.wanted({}, platforms))
        self.assertTrue(npm.wanted({"os": ["win32"], "cpu": ["x64"]}, platforms))
        self.assertFalse(npm.wanted({"os": ["darwin"], "cpu": ["x64"]}, platforms))
        self.assertFalse(npm.wanted({"os": ["linux"], "cpu": ["arm64"]}, platforms))
        self.assertTrue(npm.wanted({"os": ["!win32"]}, platforms))      # linux 에는 맞는다
        self.assertFalse(npm.wanted({"os": ["!win32", "!linux"]}, platforms))

    def test_npm_lock_versions(self):
        v3 = {"lockfileVersion": 3, "packages": {
            "": {"name": "app"},
            "node_modules/a": {"version": "1.0.0", "resolved": "https://r/a/-/a-1.0.0.tgz"},
            "node_modules/a/node_modules/@s/b": {"version": "2.0.0", "resolved": "https://r/@s/b/-/b-2.0.0.tgz"},
            "node_modules/local": {"resolved": "packages/local", "link": True},
            "node_modules/git": {"version": "1.0.0", "resolved": "git+ssh://git@github.com/x/y.git#abc"}}}
        found, skipped = npm.lock_packages(v3)
        self.assertEqual([p["name"] for p in found], ["a", "@s/b"])
        self.assertEqual(len(skipped), 1)
        v1 = {"lockfileVersion": 1, "dependencies": {"a": {"version": "1.0.0", "resolved": "https://r/a/-/a-1.0.0.tgz",
                                                           "dependencies": {"b": {"resolved": "https://r/b/-/b-1.tgz"}}}}}
        self.assertEqual([p["name"] for p in npm.lock_packages(v1)[0]], ["a", "b"])

    def test_docker_ref(self):
        r = docker.parse_ref("nginx:1.27")
        self.assertEqual((r.registry, r.repo, r.tag, r.name), ("docker.io", "library/nginx", "1.27", "nginx"))
        self.assertEqual(r.base, "https://registry-1.docker.io/v2/library/nginx")
        r = docker.parse_ref("bitnami/redis")
        self.assertEqual((r.repo, r.tag, r.name), ("bitnami/redis", "latest", "bitnami/redis"))
        r = docker.parse_ref("ghcr.io/org/app:v1")
        self.assertEqual((r.registry, r.repo, r.tag, r.name), ("ghcr.io", "org/app", "v1", "org/app"))
        r = docker.parse_ref("localhost:5000/app@sha256:" + "a" * 64)
        self.assertEqual((r.registry, r.repo, r.tag), ("localhost:5000", "app", None))
        self.assertTrue(r.base.startswith("http://localhost:5000/"))
        for bad in ("Nginx:1", "nginx:", "a b"):
            with self.assertRaises(BundlerError):
                docker.parse_ref(bad)

    def test_docker_platforms(self):
        cfg = load_config()
        self.assertEqual(docker.wanted_platforms(select_platforms(cfg, "linux-x86_64,windows-x86_64")), ["linux/amd64"])
        self.assertEqual(docker.wanted_platforms(select_platforms(cfg, "windows-x86_64")), ["linux/amd64"])
        self.assertEqual(docker.wanted_platforms(select_platforms(cfg, "linux-x86_64,linux-aarch64")),
                         ["linux/amd64", "linux/arm64"])

    def test_pypi_helpers(self):
        self.assertEqual(pypi.parse_versions("3.11, 3.12"), ["3.11", "3.12"])
        with self.assertRaises(BundlerError):
            pypi.parse_versions("3")
        html = '<a href="x">demo-1.0-py3-none-any.whl</a><a href="y">demo-0.9.tar.gz</a>'
        self.assertEqual(pypi.index_versions(html), ["1.0", "0.9"])
        cfg = load_config()
        win, = select_platforms(cfg, "windows-x86_64")
        self.assertEqual(pypi.marker_env(win, "3.12")["sys_platform"], "win32")
        self.assertEqual(pypi.marker_env(win, "3.12")["python_version"], "3.12")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nbt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.out = self.tmp / "out"


class NpmTests(Base):
    def _registry(self) -> Path:
        root = self.tmp / "registry"
        for name, ver in (("left", "1.0.0"), ("native-linux-x64", "1.0.0"), ("native-darwin-arm64", "1.0.0")):
            d = root / name / "-"
            d.mkdir(parents=True)
            (d / f"{name}-{ver}.tgz").write_bytes(npm_tarball(name, ver))
        (root / "left.json").write_text(json.dumps({"versions": {"1.0.0": {}, "1.1.0": {}}}))
        return root

    def _lock(self, base: str, root: Path, bad_integrity=False) -> Path:
        def entry(name, **extra):
            data = (root / name / "-" / f"{name}-1.0.0.tgz").read_bytes()
            digest = base64.b64encode(hashlib.sha512(b"x" if bad_integrity else data).digest()).decode()
            return {"version": "1.0.0", "resolved": f"{base}/{name}/-/{name}-1.0.0.tgz", "integrity": "sha512-" + digest, **extra}
        lock = self.tmp / "app" / "package-lock.json"
        lock.parent.mkdir(exist_ok=True)
        lock.write_text(json.dumps({"name": "app", "lockfileVersion": 3, "packages": {
            "": {"name": "app", "dependencies": {"left": "^1.0.0"}},
            "node_modules/left": entry("left"),
            "node_modules/native-linux-x64": entry("native-linux-x64", os=["linux"], cpu=["x64"], optional=True),
            "node_modules/native-darwin-arm64": entry("native-darwin-arm64", os=["darwin"], cpu=["arm64"], optional=True)}}))
        return lock

    def test_lock_file_collect_then_upload(self):
        root = self._registry()
        with FakeServer(root) as srv:
            rc, text = run_cli("collect", "--input", self._lock(srv.url, root), "--out", self.out,
                               "--platforms", "linux-x86_64", "--no-proxy")
        self.assertEqual(rc, 0, text)
        self.assertTrue((self.out / "_npm/left/left-1.0.0.tgz").is_file())
        self.assertTrue((self.out / "_npm/native-linux-x64/native-linux-x64-1.0.0.tgz").is_file())
        self.assertFalse((self.out / "_npm/native-darwin-arm64").exists())          # 다른 플랫폼
        self.assertTrue((self.out / "_samples/npm-00/package-lock.json").is_file())
        self.assertIn("npm 패키지 2개", (self.out / "_report.txt").read_text(encoding="utf-8"))
        self.assertIn("_npm/left/left-1.0.0.tgz", (self.out / "_artifacts.csv").read_text(encoding="utf-8"))

        with FakeServer(self.tmp / "nx", nexus_repo="maven-hosted", other_repos={"npm-hosted": "npm"},
                        allow_redeploy=False, auth=("admin", "pw")) as nexus:
            args = ["upload", "--dir", self.out, "--url", nexus.url, "--user", "admin", "--password", "pw",
                    "--report", self.tmp / "r.json"]
            rc, text = run_cli(*args)                       # npm 저장소를 지정하지 않으면 올리지 않고 알려 준다
            self.assertEqual(rc, 1)
            self.assertIn("--npm-repo", text)
            rc, text = run_cli(*args, "--npm-repo", "maven-hosted")   # 형식이 다른 저장소
            self.assertEqual(rc, 1)
            self.assertIn("npm hosted 가 아닙니다", text)
            rc, text = run_cli(*args, "--npm-repo", "npm-hosted")
            self.assertEqual(rc, 0, text)
            self.assertEqual(sorted(nexus.components), [("npm-hosted", "npm.asset", "left-1.0.0.tgz"),
                                                        ("npm-hosted", "npm.asset", "native-linux-x64-1.0.0.tgz")])
            rc, text = run_cli(*args, "--npm-repo", "npm-hosted")     # 재배포 금지: 건너뛰고 성공
            self.assertEqual(rc, 0, text)
            self.assertEqual(len(json.loads((self.tmp / "r.json").read_text())["skipped"]), 2)

            # GUI 의 종류별 탭: 폴더에 다른 종류가 섞여 있어도 고른 종류만 올리고, 다른 저장소는 묻지 않는다
            jar = self.out / "org/demo/demo/1.0/demo-1.0.pom"
            jar.parent.mkdir(parents=True)
            jar.write_text("<project/>")
            rc, text = run_cli(*args, "--npm-repo", "npm-hosted", "--kinds", "npm", "--dry-run")
            self.assertEqual(rc, 0, text)
            self.assertIn("npm 패키지 2개", text)
            self.assertNotIn("--repo", text)
            rc, text = run_cli(*args, "--pypi-repo", "pypi-hosted", "--kinds", "pypi", "--dry-run")
            self.assertEqual(rc, 1)
            self.assertIn("올릴 Python 패키지 이(가) 없습니다", text)

    def test_checksum_mismatch_is_a_problem(self):
        root = self._registry()
        with FakeServer(root) as srv:
            rc, text = run_cli("collect", "--input", self._lock(srv.url, root, bad_integrity=True), "--out", self.out,
                               "--platforms", "linux-x86_64", "--no-proxy")
        self.assertEqual(rc, 2, text)
        self.assertIn("체크섬", text)
        self.assertFalse(list((self.out / "_npm").rglob("*.tgz")) if (self.out / "_npm").exists() else [])

    def test_missing_package_is_reported_before_download(self):
        root = self._registry()
        (root / "left").rename(root / "left-files")
        (root / "left.json").rename(root / "left")           # GET /left → 패키지 정보
        with FakeServer(root) as srv:
            cfg = json.loads(write_repos_config(self.tmp / "repos.json", srv.url).read_text())
            cfg["npm_registry"] = srv.url + "/"
            (self.tmp / "repos.json").write_text(json.dumps(cfg))
            rc, text = run_cli("collect", "--npm", "left@9.9.9", "--npm", "no-such-pkg@1.0.0", "--out", self.out,
                               "--repos-config", self.tmp / "repos.json", "--no-proxy")
        self.assertEqual(rc, 1, text)
        self.assertIn("버전 9.9.9 은(는) 없습니다: left@9.9.9", text)
        self.assertIn("최근 버전: 1.0.0, 1.1.0", text)
        self.assertIn("이런 npm 패키지는 없습니다: no-such-pkg", text)
        self.assertFalse(self.out.exists())


class PypiTests(Base):
    def _config(self, srv) -> Path:
        cfg = json.loads(write_repos_config(self.tmp / "repos.json", srv.url).read_text())
        cfg["pypi_index"] = srv.url + "/simple/"
        (self.tmp / "repos.json").write_text(json.dumps(cfg))
        return self.tmp / "repos.json"

    def test_requirements_use_target_platform_markers_then_upload(self):
        root = self.tmp / "pypi"
        build_fake_pypi(root, {
            "demo": ("1.0", ("dep", 'winonly; sys_platform == "win32"', 'linuxonly; sys_platform == "linux"',
                             'oldpy; python_version < "3.0"')),
            "dep": ("2.0", ()), "winonly": ("1.0", ()), "linuxonly": ("1.0", ()), "oldpy": ("1.0", ())})
        req = self.tmp / "requirements.txt"
        req.write_text("demo==1.0\n")
        with FakeServer(root) as srv:
            rc, text = run_cli("collect", "--input", req, "--out", self.out, "--repos-config", self._config(srv),
                               "--platforms", "linux-x86_64", "--python-versions", "3.12", "--no-proxy",
                               "--work-dir", self.tmp / "work")
        self.assertEqual(rc, 0, text)
        names = sorted(f.name.split("-")[0] for f in (self.out / "_pypi").iterdir())
        self.assertEqual(names, ["demo", "dep", "linuxonly"])   # 수집 PC 가 아니라 대상(linux) 기준으로 골랐다
        self.assertTrue((self.out / "_samples/pypi-00/requirements.txt").is_file())

        with FakeServer(self.tmp / "nx", nexus_repo="maven-hosted", other_repos={"pypi-hosted": "pypi"}) as nexus:
            rc, text = run_cli("upload", "--dir", self.out, "--url", nexus.url, "--pypi-repo", "pypi-hosted",
                               "--report", self.tmp / "r.json")
            self.assertEqual(rc, 0, text)
            self.assertEqual(sorted(c[2].split("-")[0] for c in nexus.components), ["demo", "dep", "linuxonly"])
            self.assertTrue(all(c[:2] == ("pypi-hosted", "pypi.asset") for c in nexus.components))

    def test_missing_package_is_reported_before_download(self):
        root = self.tmp / "pypi"
        build_fake_pypi(root, {"demo": ("1.0", ())})
        with FakeServer(root) as srv:
            rc, text = run_cli("collect", "--pypi", "demo==9.9", "--pypi", "no-such-pkg", "--out", self.out,
                               "--repos-config", self._config(srv), "--no-proxy")
        self.assertEqual(rc, 1, text)
        self.assertIn("버전 9.9 은(는) 없습니다: demo==9.9", text)
        self.assertIn("최근 버전: 1.0", text)
        self.assertIn("이런 Python 패키지는 없습니다: no-such-pkg", text)
        self.assertFalse(self.out.exists())


class DockerTests(Base):
    def test_collect_selected_platform_then_push(self):
        with FakeRegistry() as hub, FakeRegistry(auth=("admin", "pw")) as nexus:
            digests = hub.add_image("team/app", "1.0")
            rc, text = run_cli("collect", "--image", f"{hub.host}/team/app:1.0", "--out", self.out,
                               "--platforms", "linux-x86_64,windows-x86_64", "--no-proxy")
            self.assertEqual(rc, 0, text)
            images = json.loads((self.out / "_docker/images.json").read_text())
            self.assertEqual([m["digest"] for m in images[0]["manifests"]], [digests["amd64"]])
            self.assertEqual(sorted(f.name for f in (self.out / "_docker").iterdir()),
                             ["images.json", "team_app_1.0_linux-amd64.tar"])
            self.assertEqual(check_docker_archive(self.out / "_docker/team_app_1.0_linux-amd64.tar"),
                             [f"{hub.host}/team/app:1.0"])   # docker load 로 읽힌다

            args = ["upload", "--dir", self.out, "--url", "http://127.0.0.1:1", "--user", "admin", "--password", "pw",
                    "--no-proxy", "--report", self.tmp / "r.json"]
            rc, text = run_cli(*args)
            self.assertEqual(rc, 1)
            self.assertIn("--docker-url", text)
            # Nexus REST 주소는 닿지 않아도(사전 점검 대상 저장소가 없으므로) 레지스트리 주소만으로 올라간다
            with FakeServer(self.tmp / "nx", nexus_repo="maven-hosted") as rest:
                args[args.index("http://127.0.0.1:1")] = rest.url
                rc, text = run_cli(*args, "--docker-url", f"http://{nexus.host}")
                self.assertEqual(rc, 0, text)
                self.assertIn(f"docker pull {nexus.host}/team/app:1.0", text)
                media, body = nexus.manifests[("team/app", "1.0")]
                self.assertEqual("sha256:" + hashlib.sha256(body).hexdigest(), digests["amd64"])   # 한 플랫폼이면 그대로
                self.assertEqual(len(nexus.blobs), 3)
                rc, text = run_cli(*args, "--docker-url", f"http://{nexus.host}")                 # 다시 올리면 블롭은 건너뜀
                self.assertEqual(rc, 0, text)
                self.assertEqual(len(json.loads((self.tmp / "r.json").read_text())["skipped"]), 3)
                bad = [a if a != "pw" else "wrong" for a in args]
                rc, text = run_cli(*bad, "--docker-url", f"http://{nexus.host}")
                self.assertEqual(rc, 1)
                self.assertIn("인증", text)

    def test_two_platforms_are_pushed_as_a_list(self):
        with FakeRegistry() as hub, FakeRegistry() as nexus, FakeServer(self.tmp / "nx", nexus_repo="m") as rest:
            digests = hub.add_image("app", "2", archs=("amd64", "arm64", "s390x"))
            rc, text = run_cli("collect", "--image", f"{hub.host}/app:2", "--out", self.out,
                               "--platforms", "linux-x86_64,linux-aarch64", "--no-proxy")
            self.assertEqual(rc, 0, text)
            for arch in ("amd64", "arm64"):
                check_docker_archive(self.out / f"_docker/app_2_linux-{arch}.tar")
            rc, text = run_cli("upload", "--dir", self.out, "--url", rest.url, "--docker-url", nexus.host, "--no-proxy",
                               "--report", self.tmp / "r.json")
            self.assertEqual(rc, 0, text)
            index = json.loads(nexus.manifests[("app", "2")][1])
            self.assertEqual([m["digest"] for m in index["manifests"]], [digests["amd64"], digests["arm64"]])

    def test_missing_image_and_platform(self):
        with FakeRegistry() as hub:
            hub.add_image("app", "1", archs=("s390x",))
            rc, text = run_cli("collect", "--image", f"{hub.host}/nope:1", "--out", self.out, "--no-proxy")
            self.assertEqual(rc, 1)
            self.assertIn("이런 Docker 이미지는 없습니다", text)
            self.assertFalse(self.out.exists())
            rc, text = run_cli("collect", "--image", f"{hub.host}/app:1", "--out", self.out, "--no-proxy")
            self.assertEqual(rc, 1)
            self.assertIn("있는 플랫폼: linux/s390x", text)


if __name__ == "__main__":
    unittest.main()
