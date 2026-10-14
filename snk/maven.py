"""Maven 수집기: 빈 로컬 저장소에서 mvn 을 실제로 실행해 의존성/플러그인을 모두 내려받는다."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from .common import GAV, MAVEN_DEPENDENCY_PLUGIN, BundlerError, log, log_tail, run_cmd

_OMITTED_RE = re.compile(r"\(([^\s()]+) - [^)]*omitted for (?:conflict|duplicate)")
_DIRECT_RE = re.compile(r"^[+\\]- ([^\s()]+)", re.M)
_SAFE_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")


def parse_tree_coord(coord: str) -> GAV | None:
    """'g:a:type:version:scope' 또는 'g:a:type:classifier:version:scope' → GAV."""
    parts = coord.split(":")
    if len(parts) == 5:
        g, a, _type, v, _scope = parts
    elif len(parts) == 6:
        g, a, _type, _cls, v, _scope = parts
    else:
        return None
    return GAV(g, a, v)


def parse_tree(text: str) -> tuple[set[GAV], set[GAV]]:
    """dependency:tree -Dverbose 출력에서 (직접 의존성, 충돌/중복으로 탈락한 노드) 를 뽑는다."""
    direct = {g for c in _DIRECT_RE.findall(text) if (g := parse_tree_coord(c))}
    losers = {g for c in _OMITTED_RE.findall(text) if (g := parse_tree_coord(c))}
    return direct, losers


@dataclass
class MavenResult:
    m2: Path
    direct: set[GAV] = field(default_factory=set)
    losers: set[GAV] = field(default_factory=set)


def write_settings(directory: Path, repos: list[dict]) -> tuple[Path, Path]:
    """사용자 ~/.m2/settings.xml 의 영향을 받지 않도록 전용 settings 를 만든다."""
    directory.mkdir(parents=True, exist_ok=True)

    def repo_xml(tag: str) -> str:
        return "".join(
            f"<{tag}><id>{escape(r['id'])}</id><url>{escape(r['url'])}</url>"
            f"<releases><enabled>true</enabled></releases><snapshots><enabled>false</enabled></snapshots></{tag}>"
            for r in repos)

    settings = directory / "settings.xml"
    settings.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<settings>\n  <profiles><profile><id>bundler</id>\n'
        f"    <repositories>{repo_xml('repository')}</repositories>\n"
        f"    <pluginRepositories>{repo_xml('pluginRepository')}</pluginRepositories>\n"
        "  </profile></profiles>\n  <activeProfiles><activeProfile>bundler</activeProfile></activeProfiles>\n</settings>\n",
        encoding="utf-8")
    # 전역 settings 는 비워서 Maven 3.8+ 기본 http 차단 미러 등의 영향을 없앤다.
    global_settings = directory / "global-settings.xml"
    global_settings.write_text('<?xml version="1.0" encoding="UTF-8"?>\n<settings/>\n', encoding="utf-8")
    return settings, global_settings


def base_command(mvn: str, settings: Path, global_settings: Path, m2: Path,
                 platform: dict | None, host_key: str) -> list[str]:
    cmd = [mvn, "-B", "-ntp", "-s", str(settings), "-gs", str(global_settings), f"-Dmaven.repo.local={m2}"]
    if platform and platform["key"] != host_key:
        # 다른 플랫폼 기준으로 해석되도록 os 속성을 덮어쓴다 (프로필 활성화, os-maven-plugin 용 best effort).
        cmd += [f"-Dos.name={platform['os_name']}", f"-Dos.arch={platform['os_arch']}",
                f"-Dos.detected.name={platform['detected_name']}",
                f"-Dos.detected.arch={platform['detected_arch']}",
                f"-Dos.detected.classifier={platform['detected_classifier']}"]
    return cmd


_OS_JVM_PROPS = ("-Dos.name=", "-Dos.arch=")


_os_override_broken = False  # 한 번 확인되면 이후 실행에서는 처음부터 os.name/os.arch 를 뺀다.


def _run_mvn(cmd: list[str], *, logfile: Path) -> int:
    global _os_override_broken
    without_os = [c for c in cmd if not c.startswith(_OS_JVM_PROPS)]
    if _os_override_broken:
        return run_cmd(without_os, logfile=logfile)
    rc = run_cmd(cmd, logfile=logfile)
    if rc != 0 and len(without_os) != len(cmd) and "SocketOptions" in log_tail(logfile, 80):
        # JDK 12+ 는 os.name 으로 소켓 구현 클래스를 고르므로 다른 OS 이름을 주면 첫 HTTP 연결에서 죽는다.
        log.warning("이 JDK 에서는 os.name 덮어쓰기가 동작하지 않아 os.detected.* 만으로 다시 실행합니다 "
                    "(<os> 프로필 활성화는 호스트 기준, 네이티브 파일은 native_hints 보강에 의존).")
        _os_override_broken = True
        rc = run_cmd(without_os, logfile=logfile)
    return rc


def resolve_project(pom: Path, work: Path, repos: list[dict], platform: dict, host_key: str,
                    mvn: str, extra_goals: str | None, logdir: Path) -> MavenResult:
    settings, global_settings = write_settings(work / "maven-settings", repos)
    m2 = work / "m2"
    m2.mkdir(parents=True, exist_ok=True)
    base = base_command(mvn, settings, global_settings, m2, platform, host_key) + ["-f", str(pom)]
    logfile = logdir / f"maven-{platform['key']}.log"

    log.info("Maven 해석 [%s]: %s", platform["key"], pom)
    rc = _run_mvn(base + [f"{MAVEN_DEPENDENCY_PLUGIN}:go-offline"], logfile=logfile)
    if rc != 0:
        raise BundlerError(f"mvn dependency:go-offline 실패 (종료 코드 {rc}). 로그: {logfile}\n{log_tail(logfile)}")

    tree_file = work / f"maven-tree-{platform['key']}.txt"
    rc = _run_mvn(base + [f"{MAVEN_DEPENDENCY_PLUGIN}:tree", "-Dverbose", "-DoutputType=text",
                         f"-DoutputFile={tree_file}"], logfile=logfile)
    result = MavenResult(m2=m2)
    if rc == 0 and tree_file.exists():
        result.direct, result.losers = parse_tree(tree_file.read_text(encoding="utf-8", errors="replace"))
    else:
        log.warning("dependency:tree 실패 - 탈락 버전 수집이 건너뛰어집니다 (로그: %s)", logfile)

    if extra_goals:
        rc = _run_mvn(base + shlex.split(extra_goals), logfile=logfile)
        if rc != 0:
            raise BundlerError(f"추가 Maven 목표 실패 ({extra_goals!r}, 종료 코드 {rc}). 로그: {logfile}\n{log_tail(logfile)}")
    return result


# ----------------------------------------------------------- 좌표 목록 해석(리액터)

def _dependency_xml(gav: GAV, pom_only: bool = False) -> str:
    type_xml = "<type>pom</type>" if pom_only else ""
    return (f"<dependency><groupId>{escape(gav.group)}</groupId><artifactId>{escape(gav.artifact)}</artifactId>"
            f"<version>{escape(gav.version)}</version>{type_xml}</dependency>")


def synthetic_pom(gavs: list[GAV]) -> str:
    """주어진 좌표들을 직접 의존성으로 갖는 단일 pom.xml (샘플/검증용)."""
    deps = "\n    ".join(_dependency_xml(g) for g in gavs)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            "  <modelVersion>4.0.0</modelVersion>\n"
            "  <groupId>bundler.tmp</groupId><artifactId>coordinates</artifactId><version>1</version>\n"
            f"  <dependencies>\n    {deps}\n  </dependencies>\n</project>\n")


def write_reactor(directory: Path, gavs: list[GAV], pom_only: set[GAV] | None = None) -> Path:
    """좌표 하나당 모듈 하나인 리액터를 만든다. 각 좌표는 서로 독립적으로(버전 충돌 없이) 해석된다."""
    directory.mkdir(parents=True, exist_ok=True)
    modules = []
    for i, gav in enumerate(gavs):
        name = f"m{i}"
        modules.append(f"<module>{name}</module>")
        mdir = directory / name
        mdir.mkdir(exist_ok=True)
        (mdir / "pom.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
            f"  <modelVersion>4.0.0</modelVersion>\n  <groupId>bundler.tmp</groupId><artifactId>{name}</artifactId>"
            f"<version>1</version>\n  <dependencies>{_dependency_xml(gav, gav in (pom_only or set()))}</dependencies>\n</project>\n",
            encoding="utf-8")
    pom = directory / "pom.xml"
    pom.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n  <groupId>bundler.tmp</groupId><artifactId>reactor</artifactId>"
        f"<version>1</version><packaging>pom</packaging>\n  <modules>{''.join(modules)}</modules>\n</project>\n",
        encoding="utf-8")
    return pom


def resolve_coordinates(gavs: list[GAV], work: Path, repos: list[dict], platform: dict | None,
                        host_key: str, mvn: str, logdir: Path, tag: str,
                        pom_only: set[GAV] | None = None, strict: bool = False) -> int:
    """좌표 목록을 Maven 으로 해석한다. 일부가 실패해도 계속 진행(-fae)하며, strict 이면 실패 시 예외."""
    gavs = sorted(set(gavs))
    if not gavs:
        return 0
    settings, global_settings = write_settings(work / "maven-settings", repos)
    m2 = work / "m2"
    m2.mkdir(parents=True, exist_ok=True)
    pom = write_reactor(work / f"maven-reactor-{tag}", gavs, pom_only)
    logfile = logdir / f"maven-{tag}.log"
    log.info("Maven 해석 [%s]: 좌표 %d개", tag, len(gavs))
    cmd = base_command(mvn, settings, global_settings, m2, platform, host_key) + [
        "-fae", "-f", str(pom), f"{MAVEN_DEPENDENCY_PLUGIN}:go-offline"]
    rc = _run_mvn(cmd, logfile=logfile)
    if rc != 0:
        if strict:
            raise BundlerError(f"Maven 좌표 해석 실패 (종료 코드 {rc}). 로그: {logfile}\n{log_tail(logfile)}")
        log.warning("Maven 좌표 해석 중 일부 실패 [%s] (로그: %s)", tag, logfile)
    return rc
