import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from snk import enrich, maven, normalize  # noqa: E402
from snk.common import (GAV, BundlerError, file_matches_platforms, is_junk, load_config,  # noqa: E402
                             select_platforms, version_key)

TREE = """\
app:input:jar:1
+- com.example:lib-a:jar:1.0:compile
|  \\- com.example:lib-b:jar:1.0:compile
+- (com.example:lib-b:jar:2.0:compile - omitted for conflict with 1.0)
|  \\- (com.example:lib-b:jar:1.0:compile - version managed from 0.9; omitted for duplicate)
\\- com.example:lib-c:jar:linux-x86_64:1.0:runtime
"""


class UnitTests(unittest.TestCase):
    def test_gav(self):
        g = GAV.parse("org.foo:bar:1.2")
        self.assertEqual(g.file("jar"), "org/foo/bar/1.2/bar-1.2.jar")
        self.assertEqual(g.file("jar", "linux"), "org/foo/bar/1.2/bar-1.2-linux.jar")
        with self.assertRaises(BundlerError):
            GAV.parse("a:b")
        self.assertLess(version_key("1.9"), version_key("1.10"))

    def test_tree_parsing(self):
        direct, losers = maven.parse_tree(TREE)
        self.assertEqual(direct, {GAV("com.example", "lib-a", "1.0"), GAV("com.example", "lib-c", "1.0")})
        self.assertIn(GAV("com.example", "lib-b", "2.0"), losers)
        self.assertIn(GAV("com.example", "lib-b", "1.0"), losers)

    def test_platform_filter(self):
        cfg = load_config()
        both = select_platforms(cfg, None)
        self.assertEqual([p["key"] for p in both], ["linux-x86_64", "windows-x86_64"])
        ok = lambda n: file_matches_platforms(n, both)  # noqa: E731
        self.assertTrue(ok("lib-1.0.jar"))
        self.assertTrue(ok("lib-1.0-natives-linux.jar"))
        self.assertTrue(ok("lib-1.0-natives-windows.jar"))
        self.assertTrue(ok("tcnative-2.0-linux-x86_64.jar"))
        self.assertFalse(ok("lib-1.0-natives-macos.jar"))
        self.assertFalse(ok("tcnative-2.0-osx-x86_64.jar"))
        self.assertFalse(ok("lib-1.0-linux-aarch_64.jar"))
        self.assertFalse(ok("lib-1.0-linux-arm64.jar"))
        self.assertFalse(ok("lib-1.0-linux-x86.jar"))
        with self.assertRaises(BundlerError):
            select_platforms(cfg, "plan9-x86_64")

    def test_junk(self):
        for n in ("_remote.repositories", "maven-metadata-central.xml", "x-1.0.jar.sha1", "x.pom.lastUpdated",
                  "resolver-status.properties", "a.jar.part"):
            self.assertTrue(is_junk(n), n)
        for n in ("x-1.0.jar", "x-1.0.pom", "x-1.0.module", "x-1.0.jar.asc"):
            self.assertFalse(is_junk(n), n)

    def test_pom_and_props(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pom"
            p.write_text(
                '<project xmlns="http://maven.apache.org/POM/4.0.0"><parent><groupId>g</groupId><artifactId>p</artifactId>'
                "<version>1.0</version></parent><artifactId>a</artifactId><packaging>bundle</packaging>"
                "<properties><bom.version>9.9</bom.version></properties>"
                "<dependencyManagement><dependencies><dependency><groupId>g</groupId><artifactId>bom</artifactId>"
                "<version>${bom.version}</version><type>pom</type><scope>import</scope></dependency></dependencies>"
                "</dependencyManagement></project>")
            info = enrich.parse_pom(p)
            self.assertEqual(info.parent, GAV("g", "p", "1.0"))
            self.assertEqual(info.group, "g")
            self.assertEqual(info.version, "1.0")
            self.assertEqual(info.packaging, "bundle")
            self.assertEqual(info.imports, [("g", "bom", "${bom.version}")])
            self.assertEqual(enrich.resolve_props("${bom.version}", info.props), "9.9")
            self.assertIsNone(enrich.resolve_props("${nope}", info.props))

    def test_pom_html_entity(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.pom"
            p.write_text(
                "<project><parent><groupId>g</groupId><artifactId>p</artifactId><version>1.0</version></parent>"
                "<artifactId>a</artifactId><name>Laugst&oslash;l &amp; co &nosuch;</name></project>")
            info = enrich.parse_pom(p)
            self.assertEqual(info.parent, GAV("g", "p", "1.0"))

    def test_module_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.module"
            p.write_text('{"variants":[{"files":[{"url":"x-1.jar"},{"url":"../evil/x.jar"}]},'
                         '{"attributes":{"org.gradle.category":"documentation"},"files":[{"url":"x-1-sources.jar"}]}]}')
            self.assertEqual(enrich.module_files(p), ["x-1.jar"])
            self.assertEqual(enrich.module_files(p, with_sources=True), ["x-1.jar", "x-1-sources.jar"])

    def test_gradle_cache_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            ghome, stage = Path(d) / "gh", Path(d) / "stage"
            stage.mkdir()
            base = ghome / "caches/modules-2/files-2.1/org.foo.bar/lib/1.0"
            for h, name in (("aaa", "lib-1.0.jar"), ("bbb", "lib-1.0.pom"), ("ccc", "lib-1.0.module")):
                (base / h).mkdir(parents=True, exist_ok=True)
                (base / h / name).write_text(name)
            snap = ghome / "caches/modules-2/files-2.1/org.foo/snap/1.0-SNAPSHOT/zzz"
            snap.mkdir(parents=True)
            (snap / "snap-1.0-SNAPSHOT.jar").write_text("x")
            origins = {}
            copied, snaps = normalize.import_gradle_cache(ghome, stage, origins)
            self.assertEqual(copied, 3)
            self.assertTrue((stage / "org/foo/bar/lib/1.0/lib-1.0.jar").exists())
            self.assertEqual(snaps, {"org.foo/snap/1.0-SNAPSHOT"})
            self.assertEqual(origins["org/foo/bar/lib/1.0/lib-1.0.pom"], "gradle")
            gavs = normalize.scan_gavs(stage)
            self.assertEqual(set(gavs), {GAV("org.foo.bar", "lib", "1.0")})

    def test_maven_local_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            m2, stage = Path(d) / "m2", Path(d) / "stage"
            stage.mkdir()
            base = m2 / "org/foo/lib/1.0"
            base.mkdir(parents=True)
            for n in ("lib-1.0.jar", "lib-1.0.pom", "lib-1.0.jar.sha1", "_remote.repositories", "lib-1.0.pom.lastUpdated"):
                (base / n).write_text(n)
            (m2 / "org/foo/lib/maven-metadata-central.xml").write_text("x")
            copied, _ = normalize.import_maven_local(m2, stage, {})
            self.assertEqual(copied, 2)
            self.assertEqual(sorted(p.name for p in (stage / "org/foo/lib/1.0").iterdir()), ["lib-1.0.jar", "lib-1.0.pom"])

    def test_maven_reactor_and_settings(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gavs = [GAV("a", "b", "1"), GAV("c", "d", "2")]
            pom = maven.write_reactor(d / "r", gavs, pom_only={gavs[1]})
            self.assertIn("<module>m1</module>", pom.read_text())
            self.assertIn("<type>pom</type>", (d / "r/m1/pom.xml").read_text())
            self.assertNotIn("<type>pom</type>", (d / "r/m0/pom.xml").read_text())
            settings, gs = maven.write_settings(d / "s", load_config()["repositories"])
            text = settings.read_text()
            self.assertIn("repo.maven.apache.org", text)
            self.assertIn("maven.google.com", text)
            self.assertIn("plugins.gradle.org", text)
            self.assertIn("<pluginRepositories>", text)


if __name__ == "__main__":
    unittest.main()
