import contextlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import sneakernet  # noqa: E402
from helpers import FAKE_MVN, FakeServer, build_fake_repo, write_repos_config  # noqa: E402
from snk import enrich, normalize  # noqa: E402
from snk.common import GAV, HttpClient, load_config, select_platforms  # noqa: E402

HAVE_GRADLE = shutil.which("gradle") is not None


def run_cli(*argv) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = sneakernet.main([str(a) for a in argv])
    return rc, buf.getvalue()


class PipelineBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="nbt-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo_dir = build_fake_repo(self.tmp / "repo")


class EnrichTests(PipelineBase):
    def test_enrich_fills_parent_bom_module_natives_and_checks_checksum(self):
        stage = self.tmp / "stage"
        # Gradle 이 pom 만 받아 둔 상황을 흉내: lib-a 의 pom 한 개만 있다.
        src = self.repo_dir / "com/example/lib-a/1.0/lib-a-1.0.pom"
        dst = stage / "com/example/lib-a/1.0/lib-a-1.0.pom"
        dst.parent.mkdir(parents=True)
        shutil.copy2(src, dst)
        cfg = load_config()
        platforms = select_platforms(cfg, None)
        with FakeServer(self.repo_dir) as srv:
            repos = [{"id": "fake", "url": srv.url + "/"}]
            seeds = {GAV("com.example", "lib-a", "1.0"), GAV("com.example", "lib-b", "2.0"),
                     GAV("io.netty", "netty-tcnative-boringssl-static", "2.0.0")}
            e = enrich.Enricher(stage, repos, HttpClient(), platforms, cfg["native_hints"], {}, workers=4).run(seeds)
        self.assertEqual(e.problems, [])
        has = lambda rel: (stage / rel).exists()  # noqa: E731
        self.assertTrue(has("com/example/lib-a/1.0/lib-a-1.0.jar"))          # 대표 아티팩트
        self.assertTrue(has("com/example/parent/1.0/parent-1.0.pom"))        # parent
        self.assertTrue(has("com/example/bom/1.0/bom-1.0.pom"))              # parent 속성으로 버전이 정해진 BOM import
        self.assertTrue(has("com/example/lib-b/2.0/lib-b-2.0.module"))
        self.assertTrue(has("com/example/lib-b/2.0/lib-b-2.0-natives-linux.jar"))
        self.assertFalse(has("com/example/lib-b/2.0/lib-b-2.0-natives-macos.jar"))   # 다른 플랫폼
        self.assertFalse(has("com/example/lib-b/2.0/lib-b-2.0-sources.jar"))         # sources 제외
        base = "io/netty/netty-tcnative-boringssl-static/2.0.0/netty-tcnative-boringssl-static-2.0.0"
        self.assertTrue(has(base + "-linux-x86_64.jar"))
        self.assertTrue(has(base + "-windows-x86_64.jar"))
        self.assertFalse(has(base + "-osx-x86_64.jar"))
        self.assertFalse(list(stage.rglob("*.sha1")))

    def test_enrich_rejects_checksum_mismatch(self):
        (self.repo_dir / "com/example/lib-c/1.0/lib-c-1.0.jar.sha1").write_text("0" * 40)
        stage = self.tmp / "stage"
        stage.mkdir()
        cfg = load_config()
        with FakeServer(self.repo_dir) as srv:
            e = enrich.Enricher(stage, [{"id": "fake", "url": srv.url + "/"}], HttpClient(retries=0),
                                select_platforms(cfg, None), [], {}, workers=2).run({GAV("com.example", "lib-c", "1.0")})
        self.assertFalse((stage / "com/example/lib-c/1.0/lib-c-1.0.jar").exists())
        self.assertTrue((stage / "com/example/lib-c/1.0/lib-c-1.0.pom").exists())
        self.assertTrue(any("체크섬" in w for w in e.warnings), e.warnings)

    def test_missing_pom_is_reported(self):
        stage = self.tmp / "stage"
        stage.mkdir()
        cfg = load_config()
        with FakeServer(self.repo_dir) as srv:
            e = enrich.Enricher(stage, [{"id": "fake", "url": srv.url + "/"}], HttpClient(retries=0),
                                select_platforms(cfg, None), [], {}, workers=2).run({GAV("no.such", "thing", "1")})
        self.assertTrue(any("no.such:thing:1" in p for p in e.problems))


class UploadTests(PipelineBase):
    def _bundle(self) -> Path:
        out = self.tmp / "bundle"
        shutil.copytree(self.repo_dir, out)
        # 반입 디렉토리에는 체크섬/잡파일/_메타 가 섞여 있을 수 있다.
        (out / "_artifacts.csv").write_text("x")
        (out / "_samples").mkdir()
        (out / "_samples/pom.xml").write_text("<project/>")
        (out / "com/example/lib-a/maven-metadata-central.xml").write_text("x")
        return out

    def test_upload_order_skip_and_auth(self):
        bundle = self._bundle()
        nexus_root = self.tmp / "nexus"
        nexus_root.mkdir()
        report = self.tmp / "report.json"
        with FakeServer(nexus_root, nexus_repo="maven-hosted", allow_redeploy=False, auth=("admin", "pw")) as srv:
            args = ["upload", "--dir", bundle, "--url", srv.url, "--repo", "maven-hosted", "--user", "admin",
                    "--password", "pw", "--report", report]
            rc, out = run_cli(*args)
            self.assertEqual(rc, 0, out)
            first = json.loads(report.read_text())
            self.assertGreater(len(first["uploaded"]), 10)
            self.assertEqual(first["failed"], [])
            paths = [p for p in srv.put_log]
            self.assertFalse(any("/_" in p or p.endswith(".sha1") or "maven-metadata" in p for p in paths), paths)
            # 순서: 모든 jar 가 첫 pom 보다 먼저
            last_jar = max(i for i, p in enumerate(paths) if p.endswith(".jar"))
            first_pom = min(i for i, p in enumerate(paths) if p.endswith(".pom"))
            first_module = min(i for i, p in enumerate(paths) if p.endswith(".module"))
            last_pom = max(i for i, p in enumerate(paths) if p.endswith(".pom"))
            self.assertLess(last_jar, first_pom)
            self.assertLess(last_pom, first_module)

            # 재배포 금지 저장소에 다시 올리면 모두 건너뛰고 성공
            rc, out = run_cli(*args)
            self.assertEqual(rc, 0, out)
            second = json.loads(report.read_text())
            self.assertEqual(second["uploaded"], [])
            self.assertEqual(len(second["skipped"]), len(first["uploaded"]))

            # 잘못된 비밀번호 → 치명적 오류
            bad = [a if a != "pw" else "wrong" for a in args]
            rc, out = run_cli(*bad)
            self.assertEqual(rc, 1)

    def test_dry_run_and_wrong_repo(self):
        bundle = self._bundle()
        with FakeServer(self.tmp / "nx", nexus_repo="maven-hosted") as srv:
            rc, out = run_cli("upload", "--dir", bundle, "--url", srv.url, "--repo", "maven-hosted", "--dry-run")
            self.assertEqual(rc, 0)
            self.assertEqual(srv.put_log, [])
            rc, _ = run_cli("upload", "--dir", bundle, "--url", srv.url, "--repo", "nope",
                            "--report", self.tmp / "r.json")
            self.assertEqual(rc, 1)


def make_fake_mvn(tmp: Path) -> Path:
    path = tmp / "fake-mvn"
    path.write_text(FAKE_MVN, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    if os.name == "nt":  # Windows 는 shebang 을 실행하지 못하므로 .cmd 로 감싼다 (실제 mvn 도 mvn.cmd)
        cmd = tmp / "fake-mvn.cmd"
        cmd.write_text(f'@"{sys.executable}" "%~dp0fake-mvn" %*\r\n')
        return cmd
    return path


@unittest.skipUnless(HAVE_GRADLE, "gradle 이 필요합니다")
class CollectEndToEnd(PipelineBase):
    def _collect(self, srv, kind: str, extra_env=None):
        cfg_file = write_repos_config(self.tmp / "repos.json", srv.url)
        mvn = make_fake_mvn(self.tmp)
        mvn_log = self.tmp / "mvn.log"
        os.environ.update({"FAKE_REPO_DIR": str(self.repo_dir), "FAKE_MVN_LOG": str(mvn_log)})
        for k, v in (extra_env or {}).items():
            os.environ[k] = v
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_REPO_DIR", "FAKE_MVN_LOG", "FAKE_TREE_EXTRA")])
        out, work = self.tmp / "out", self.tmp / "work"
        inp = HERE / "data" / ("gradle-app/build.gradle" if kind == "gradle" else "maven-app/pom.xml")
        rc, text = run_cli("collect", "--input", inp, "--out", out, "--repos-config", cfg_file, "--mvn-cmd", mvn,
                           "--platforms", "linux-x86_64", "--work-dir", work, "--no-proxy")
        return rc, text, out, work, mvn_log

    def _assert_common_bundle(self, out: Path):
        has = lambda rel: (out / rel).exists()  # noqa: E731
        self.assertTrue(has("com/example/lib-a/1.0/lib-a-1.0.jar"))
        self.assertTrue(has("com/example/lib-a/1.0/lib-a-1.0.pom"))
        self.assertTrue(has("com/example/lib-c/1.0/lib-c-1.0.jar"))
        self.assertTrue(has("com/example/lib-b/1.0/lib-b-1.0.jar"))      # 탈락(또는 선택) 버전 모두
        self.assertTrue(has("com/example/lib-b/2.0/lib-b-2.0.jar"))
        self.assertTrue(has("com/example/lib-b/2.0/lib-b-2.0.module"))   # Gradle 메타데이터 보강
        self.assertTrue(has("com/example/lib-b/2.0/lib-b-2.0-natives-linux.jar"))
        self.assertFalse(has("com/example/lib-b/2.0/lib-b-2.0-natives-macos.jar"))
        self.assertTrue(has("com/example/parent/1.0/parent-1.0.pom"))
        self.assertTrue(has("com/example/bom/1.0/bom-1.0.pom"))
        self.assertTrue(has("_artifacts.csv") and has("_report.txt"))
        bad = [p.name for p in out.rglob("*") if p.is_file() and not p.relative_to(out).parts[0].startswith("_")
               and (p.name.endswith((".sha1", ".lastUpdated", ".part")) or p.name.startswith(("maven-metadata", "_remote")))]
        self.assertEqual(bad, [])

    def test_gradle_input_then_upload_then_verify(self):
        with FakeServer(self.repo_dir) as srv:
            rc, text, out, work, mvn_log = self._collect(srv, "gradle")
        self.assertEqual(rc, 0, text)
        self._assert_common_bundle(out)
        self.assertTrue((work / "logs/gradle-project-linux-x86_64.log").exists())
        # 교차 해석: Maven 쪽 (가짜 mvn) 이 리액터로 호출되었는지
        calls = mvn_log.read_text()
        self.assertIn("-fae", calls)
        self.assertIn("maven-dependency-plugin:3.8.1:go-offline", calls)
        self.assertIn("-Dmaven.repo.local=", calls)
        self.assertTrue((out / "_samples").is_dir())

        # 업로드 → 가짜 Nexus
        nexus_root = self.tmp / "nexus"
        nexus_root.mkdir()
        with FakeServer(nexus_root, nexus_repo="maven-hosted", auth=("dev", "secret")) as nexus:
            rc, text = run_cli("upload", "--dir", out, "--url", nexus.url, "--repo", "maven-hosted",
                               "--user", "dev", "--password", "secret", "--report", self.tmp / "up.json")
            self.assertEqual(rc, 0, text)
            # 검증: Gradle 이 Nexus 만 바라보고 빈 캐시에서 해석
            rc, text = run_cli("verify", "--url", nexus.url, "--repo", "maven-hosted", "--user", "dev",
                               "--password", "secret", "--samples", out / "_samples", "--tools", "gradle",
                               "--work-dir", self.tmp / "vwork")
            self.assertEqual(rc, 0, text)
            self.assertIn("[OK]", text)
            gets = [p for m, p in nexus.requests if m == "GET" and "lib-b" in p]
            self.assertTrue(gets, "Nexus 에서 실제로 내려받았어야 함")

    def test_gradle_verify_fails_when_artifact_missing_in_nexus(self):
        with FakeServer(self.repo_dir) as srv:
            rc, text, out, work, _ = self._collect(srv, "gradle")
        self.assertEqual(rc, 0, text)
        nexus_root = self.tmp / "nexus2"
        nexus_root.mkdir()
        with FakeServer(nexus_root, nexus_repo="maven-hosted") as nexus:
            run_cli("upload", "--dir", out, "--url", nexus.url, "--repo", "maven-hosted", "--report", self.tmp / "up.json")
            # 누락 상황: 선택되는 버전(lib-b 2.0)의 jar 가 Nexus 에 없다
            (nexus_root / "com/example/lib-b/2.0/lib-b-2.0.jar").unlink()
            rc, text = run_cli("verify", "--url", nexus.url, "--repo", "maven-hosted", "--samples", out / "_samples",
                               "--tools", "gradle", "--work-dir", self.tmp / "vwork2")
        self.assertEqual(rc, 1, text)
        self.assertIn("실패", text)

    def test_maven_input_with_fake_mvn_and_real_gradle_cross(self):
        extra = "   (com.example:lib-b:jar:2.0:compile - omitted for conflict with 1.0)"
        with FakeServer(self.repo_dir) as srv:
            rc, text, out, work, mvn_log = self._collect(srv, "maven", {"FAKE_TREE_EXTRA": extra})
        self.assertEqual(rc, 0, text)
        self._assert_common_bundle(out)
        calls = mvn_log.read_text()
        self.assertIn(":go-offline", calls)
        self.assertIn(":tree", calls)
        self.assertIn("-Dverbose", calls)
        # 교차 해석: 실제 Gradle 이 직접 의존성/탈락 버전을 해석했다
        self.assertTrue((work / "logs/gradle-cross.log").exists())
        self.assertTrue(list(out.glob("_samples/*/pom.xml")))

    def test_coordinate_mode(self):
        with FakeServer(self.repo_dir) as srv:
            cfg_file = write_repos_config(self.tmp / "repos.json", srv.url)
            mvn = make_fake_mvn(self.tmp)
            os.environ.update({"FAKE_REPO_DIR": str(self.repo_dir), "FAKE_MVN_LOG": str(self.tmp / "mvn.log")})
            self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_REPO_DIR", "FAKE_MVN_LOG")])
            out = self.tmp / "out"
            rc, text = run_cli("collect", "--coordinate", "com.example:lib-a:1.0", "--out", out,
                               "--repos-config", cfg_file, "--mvn-cmd", mvn, "--platforms", "linux-x86_64")
        self.assertEqual(rc, 0, text)
        self.assertTrue((out / "com/example/lib-a/1.0/lib-a-1.0.jar").exists())
        self.assertTrue((out / "com/example/lib-b/1.0/lib-b-1.0.jar").exists())
        self.assertTrue((out / "_samples/coordinates/pom.xml").exists())
        self.assertTrue((out / "_samples/coordinates/build.gradle").exists())

    def test_coordinate_that_does_not_exist_is_reported_before_download(self):
        (self.repo_dir / "com/example/lib-a/maven-metadata.xml").write_text(
            "<metadata><versioning><versions><version>1.0</version></versions></versioning></metadata>")
        err = io.StringIO()
        with FakeServer(self.repo_dir) as srv, contextlib.redirect_stderr(err):
            cfg_file = write_repos_config(self.tmp / "repos.json", srv.url)
            out = self.tmp / "out"
            rc, _ = run_cli("collect", "--coordinate", "com.example:lib-a:9.9", "--coordinate", "no.such:thing:1.0",
                               "--out", out, "--repos-config", cfg_file, "--mvn-cmd", make_fake_mvn(self.tmp),
                               "--platforms", "linux-x86_64")
        text = err.getvalue()
        self.assertEqual(rc, 1, text)
        self.assertIn("버전 9.9 은(는) 없습니다: com.example:lib-a:9.9", text)
        self.assertIn("최근 버전: 1.0", text)
        self.assertIn("이런 라이브러리는 없습니다: no.such:thing:1.0", text)
        self.assertFalse(out.exists())

    def test_wrong_version_in_pom_is_explained(self):
        from snk import collect
        from snk.common import BundlerError, HttpClient
        (self.repo_dir / "com/example/lib-a/maven-metadata.xml").write_text(
            "<metadata><versioning><versions><version>1.0</version></versions></versioning></metadata>")
        pom = self.tmp / "pom.xml"
        pom.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
                       "<groupId>my</groupId><artifactId>app</artifactId><version>1</version>\n"
                       "<properties><b.version>7.7</b.version></properties>\n<dependencies>\n"
                       "<dependency><groupId>com.example</groupId><artifactId>lib-a</artifactId><version>9.9</version></dependency>\n"
                       "<dependency><groupId>com.example</groupId><artifactId>lib-b</artifactId><version>${b.version}</version></dependency>\n"
                       "<dependency><groupId>com.example</groupId><artifactId>lib-c</artifactId><version>1.0</version></dependency>\n"
                       "</dependencies></project>\n", encoding="utf-8")
        logfile = self.tmp / "maven.log"   # 실제 Maven 은 첫 번째 실패만 알려 준다
        logfile.write_text("[ERROR] Failed to execute goal org.apache.maven.plugins:maven-dependency-plugin:3.8.1:go-offline "
                           "(default-cli) on project app: Could not resolve dependencies for project my:app:jar:1: "
                           "The following artifacts could not be resolved: com.example:lib-a:jar:9.9 (absent): "
                           "Could not find artifact com.example:lib-a:jar:9.9 in central (http://x/)\n", encoding="utf-8")
        with FakeServer(self.repo_dir) as srv:
            repos = [{"id": "fake", "url": srv.url + "/"}]
            text = str(collect.explain_failure(BundlerError("mvn 실패"), pom, logfile, repos, HttpClient(no_proxy=True)))
        self.assertIn("버전 9.9 은(는) 없습니다: com.example:lib-a:9.9 (5번째 줄)", text)
        self.assertIn("최근 버전: 1.0", text)
        self.assertIn("com.example:lib-b:7.7 (6번째 줄)", text)   # 로그에는 없지만 파일에 적힌 것도 함께
        self.assertNotIn("my:app", text)                           # 프로젝트 자신과 goal 은 좌표로 보지 않는다
        self.assertNotIn("go-offline 은", text)
        self.assertNotIn("lib-c", text)

    def test_unresolvable_dependency_is_an_error(self):
        bad = self.tmp / "bad/build.gradle"
        bad.parent.mkdir()
        bad.write_text("plugins { id 'java-library' }\ndependencies { implementation 'no.such:thing:9.9' }\n")
        with FakeServer(self.repo_dir) as srv:
            cfg_file = write_repos_config(self.tmp / "repos.json", srv.url)
            rc, text = run_cli("collect", "--input", bad, "--out", self.tmp / "out", "--repos-config", cfg_file,
                               "--platforms", "linux-x86_64", "--no-cross")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
