"""verify 서브커맨드: 업로드된 Nexus 만 바라보게 한 상태에서 Maven/Gradle 이 샘플 프로젝트를 해석하는지 확인한다.

모든 저장소를 Nexus 로 바꿔치기하고 빈 캐시에서 실행하므로, Nexus 에 없는 파일이 있으면 곧바로 실패한다.
"""
from __future__ import annotations

import getpass
import os
import shutil
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

from . import gradle, maven
from .common import (MAVEN_DEPENDENCY_PLUGIN, BundlerError, find_tool, host_platform_key, log,
                     log_tail, run_cmd)


def _is_project(d: Path) -> bool:
    return any((d / n).exists() for n in ("pom.xml", "build.gradle", "build.gradle.kts"))


def find_samples(paths: list[str], work: Path) -> list[tuple[str, Path]]:
    """(이름, 작업용으로 복사한 프로젝트 디렉토리) 목록."""
    found: list[tuple[str, Path]] = []
    for raw in paths:
        p = Path(raw).resolve()
        candidates: list[Path] = []
        if p.is_file():
            candidates.append(p)
        elif p.is_dir():
            candidates = [p] if _is_project(p) else [c for c in sorted(p.iterdir()) if c.is_dir() and _is_project(c)]
        if not candidates:
            raise BundlerError(f"샘플을 찾을 수 없습니다 (pom.xml 또는 build.gradle 이 있는 디렉토리/파일이어야 함): {p}")
        for c in candidates:
            dest = work / "samples" / f"{len(found):02d}-{c.stem if c.is_file() else c.name}"
            if c.is_file():
                dest.mkdir(parents=True)
                n = c.name.lower()
                shutil.copy2(c, dest / ("pom.xml" if n.endswith((".xml", ".pom")) else
                                        "build.gradle.kts" if n.endswith(".kts") else "build.gradle"))
                if not n.endswith((".xml", ".pom")):
                    (dest / "settings.gradle").write_text("rootProject.name = 'sample'\n", encoding="utf-8")
            else:
                shutil.copytree(c, dest, ignore=shutil.ignore_patterns(".gradle", "build", "target"))
            found.append((dest.name, dest))
    return found


def _write_maven_settings(directory: Path, nexus_base: str, user: str | None, password: str | None) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    server = ""
    if user:
        server = (f"<servers><server><id>nexus</id><username>{escape(user)}</username>"
                  f"<password>{escape(password or '')}</password></server></servers>")
    settings = directory / "settings.xml"
    settings.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<settings>\n'
        f"  <mirrors><mirror><id>nexus</id><mirrorOf>*</mirrorOf><url>{escape(nexus_base)}</url></mirror></mirrors>\n"
        f"  {server}\n</settings>\n", encoding="utf-8")
    try:
        os.chmod(settings, 0o600)
    except OSError:
        pass
    global_settings = directory / "global-settings.xml"
    global_settings.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<settings/>\n', encoding="utf-8")
    return settings, global_settings


def verify_maven(name: str, proj: Path, work: Path, mvn: str, nexus_base: str, user, password,
                 offline_build: bool) -> tuple[bool, str]:
    settings, global_settings = _write_maven_settings(work / "verify-maven-settings", nexus_base, user, password)
    m2 = work / f"verify-m2-{name}"
    shutil.rmtree(m2, ignore_errors=True)
    logfile = work / "logs" / f"verify-maven-{name}.log"
    base = [mvn, "-B", "-ntp", "-f", str(proj / "pom.xml"), "-s", str(settings), "-gs", str(global_settings),
            f"-Dmaven.repo.local={m2}"]
    rc = run_cmd(base + [f"{MAVEN_DEPENDENCY_PLUGIN}:go-offline"], logfile=logfile)
    if rc != 0:
        return False, f"의존성/플러그인 해석 실패 (로그: {logfile})\n{log_tail(logfile, 15)}"
    if offline_build:
        rc = run_cmd(base + ["-o", "package", "-DskipTests"], logfile=logfile)
        if rc != 0:
            return False, f"오프라인 빌드 실패 (로그: {logfile})\n{log_tail(logfile, 15)}"
    return True, ""


def verify_gradle(name: str, proj: Path, work: Path, grd: str, nexus_base: str, user, password,
                  host_key: str) -> tuple[bool, str]:
    ghome = work / f"verify-gradle-home-{name}"
    m2 = work / f"verify-gradle-m2-{name}"
    out_dir = work / "gradle-out" / f"verify-{name}"
    for d in (ghome, m2, out_dir):
        shutil.rmtree(d, ignore_errors=True)
    out_dir.mkdir(parents=True)
    logfile = work / "logs" / f"verify-gradle-{name}.log"
    cmd = gradle.gradle_command(grd, proj, ghome, out_dir, [{"url": nexus_base}], None, host_key,
                                init_scripts=[gradle.INIT_RESOLVE, gradle.INIT_NEXUS])
    cmd += [f"-Dmaven.repo.local={m2}", "bundlerResolveAll"]
    env = dict(os.environ, BUNDLER_NEXUS_URL=nexus_base, BUNDLER_NEXUS_USER=user or "",
               BUNDLER_NEXUS_PASSWORD=password or "")
    rc = run_cmd(cmd, logfile=logfile, env=env)
    unresolved = gradle._read_lines(out_dir / "gradle-unresolved.txt")
    if rc != 0:
        return False, f"Gradle 실행 실패 (로그: {logfile})\n{log_tail(logfile, 15)}"
    if unresolved:
        return False, "해석하지 못한 의존성:\n  " + "\n  ".join(unresolved[:20])
    return True, ""


def run(args) -> int:
    nexus_base = f"{args.url.rstrip('/')}/repository/{args.repo}/"
    password = args.password or os.environ.get("NEXUS_PASSWORD")
    if args.user and password is None and os.isatty(0):
        password = getpass.getpass(f"{args.user} 비밀번호: ")

    tools = [t.strip() for t in args.tools.split(",") if t.strip()]
    mvn = find_tool("mvn", args.mvn_cmd) if "maven" in tools else None
    grd = find_tool("gradle", args.gradle_cmd) if "gradle" in tools else None

    keep = bool(args.work_dir)
    work = Path(args.work_dir).resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="snk-verify-"))
    work.mkdir(parents=True, exist_ok=True)
    samples = find_samples(args.samples, work)
    host = host_platform_key()

    results = []
    for name, proj in samples:
        if mvn and (proj / "pom.xml").exists():
            log.info("검증 Maven: %s", name)
            ok, detail = verify_maven(name, proj, work, mvn, nexus_base, args.user, password, args.offline_build)
            results.append((name, "maven", ok, detail))
        if grd and any((proj / n).exists() for n in ("build.gradle", "build.gradle.kts")):
            log.info("검증 Gradle: %s", name)
            ok, detail = verify_gradle(name, proj, work, grd, nexus_base, args.user, password, host)
            results.append((name, "gradle", ok, detail))
    if not results:
        raise BundlerError("검증할 대상이 없습니다 (선택한 도구에 맞는 샘플이 없음).")

    print(f"\n검증 대상 Nexus: {nexus_base}")
    for name, tool, ok, detail in results:
        print(f"  [{'OK' if ok else '실패'}] {name} ({tool})")
        if not ok:
            print("    " + detail.replace("\n", "\n    "))
    failed = [r for r in results if not r[2]]
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\n결과: {len(results) - len(failed)}/{len(results)} 성공")
    return 1 if failed else 0
