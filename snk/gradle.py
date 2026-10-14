"""Gradle 수집기: init script 로 모든 resolvable configuration 을 강제 해석한다."""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .common import GAV, PKG_DIR, BundlerError, log, log_tail, run_cmd

INIT_RESOLVE = PKG_DIR / "init-resolve.groovy"
INIT_NEXUS = PKG_DIR / "init-nexus.groovy"
_SAFE_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")


@dataclass
class GradleResult:
    ghome: Path
    requested: set[GAV] = field(default_factory=set)
    direct: set[GAV] = field(default_factory=set)
    selected: set[GAV] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _read_gavs(path: Path) -> set[GAV]:
    result = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                result.add(GAV.parse(line))
            except BundlerError:
                pass
    return result


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def gradle_command(gradle: str, project_dir: Path, ghome: Path, out_dir: Path, repos: list[dict],
                   platform: dict | None, host_key: str, init_scripts: list[Path] | None = None) -> list[str]:
    cmd = [gradle, "-p", str(project_dir)]
    for init in (init_scripts or [INIT_RESOLVE]):
        cmd += ["-I", str(init)]
    cmd += ["--gradle-user-home", str(ghome), "--console=plain", "--no-daemon", "--continue",
            f"-Dbundler.out={out_dir}"]
    for i, repo in enumerate(repos):
        cmd.append(f"-Dbundler.repo.{i}={repo['url']}")
    if platform and platform["key"] != host_key:
        cmd += [f"-Dos.name={platform['os_name']}", f"-Dos.arch={platform['os_arch']}"]
    return cmd


def _run(project_dir: Path, work: Path, repos: list[dict], platform: dict | None, host_key: str,
         gradle: str, logdir: Path, tag: str) -> GradleResult:
    ghome = work / "gradle-home"
    ghome.mkdir(parents=True, exist_ok=True)
    out_dir = work / "gradle-out" / tag
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    logfile = logdir / f"gradle-{tag}.log"

    cmd = gradle_command(gradle, project_dir, ghome, out_dir, repos, platform, host_key) + ["bundlerResolveAll"]
    log.info("Gradle 해석 [%s]: %s", tag, project_dir)
    rc = run_cmd(cmd, logfile=logfile)
    result = GradleResult(
        ghome=ghome,
        requested=_read_gavs(out_dir / "gradle-requested.txt"),
        direct=_read_gavs(out_dir / "gradle-direct.txt"),
        selected=_read_gavs(out_dir / "gradle-selected.txt"),
        unresolved=_read_lines(out_dir / "gradle-unresolved.txt"),
        failures=_read_lines(out_dir / "gradle-failures.txt"),
    )
    if rc != 0:
        raise BundlerError(f"gradle 실행 실패 (종료 코드 {rc}). 로그: {logfile}\n{log_tail(logfile)}")
    return result


def resolve_project(project_dir: Path, work: Path, repos: list[dict], platform: dict, host_key: str,
                    gradle: str, logdir: Path) -> GradleResult:
    result = _run(project_dir, work, repos, platform, host_key, gradle, logdir, f"project-{platform['key']}")
    if result.unresolved:
        unique: dict[str, str] = {}
        for line in result.unresolved:  # configuration 마다 같은 실패가 반복되므로 메시지 기준으로 합친다
            unique.setdefault(line.rsplit(" : ", 1)[-1], line)
        shown = "\n  ".join(unique.values())
        raise BundlerError(f"Gradle 이 해석하지 못한 의존성이 있습니다:\n  {shown}")
    for f in result.failures:
        log.warning("Gradle configuration 해석 경고: %s", f)
    return result


# ----------------------------------------------------------- 좌표 목록 해석

def synthetic_build(gavs: list[GAV]) -> str:
    """좌표 하나당 configuration 하나 (서로 독립적으로 해석)."""
    lines = ["// 자동 생성: 좌표 목록 해석용", "configurations {"]
    safe = [g for g in gavs if all(_SAFE_PART.match(p) for p in (g.group, g.artifact, g.version))]
    for i, _ in enumerate(safe):
        lines.append(f"    create('c{i}')")
    lines += ["}", "dependencies {"]
    for i, g in enumerate(safe):
        lines.append(f"    add('c{i}', '{g}')")
    lines.append("}")
    return "\n".join(lines) + "\n"


def single_build(gav: GAV) -> str:
    """단일 좌표 샘플/검증용 build.gradle."""
    return ("plugins { id 'java-library' }\nrepositories { mavenCentral() }\n"
            f"dependencies {{ implementation '{gav}' }}\n")


def resolve_coordinates(gavs: list[GAV], work: Path, repos: list[dict], platform: dict | None,
                        host_key: str, gradle: str, logdir: Path, tag: str,
                        strict: bool = False) -> GradleResult | None:
    """좌표 목록을 Gradle 로 해석한다. strict 가 아니면 실패해도 경고만 남긴다(교차 해석 단계)."""
    gavs = sorted(set(gavs))
    if not gavs:
        return None
    proj = work / f"gradle-coords-{tag}"
    shutil.rmtree(proj, ignore_errors=True)
    proj.mkdir(parents=True)
    (proj / "settings.gradle").write_text("rootProject.name = 'bundler-coords'\n", encoding="utf-8")
    (proj / "build.gradle").write_text(synthetic_build(gavs), encoding="utf-8")
    try:
        result = _run(proj, work, repos, platform, host_key, gradle, logdir, tag)
    except BundlerError as e:
        if strict:
            raise
        log.warning("Gradle 좌표 해석 실패 [%s]: %s", tag, e)
        return None
    if strict and result.unresolved:
        shown = "\n  ".join(result.unresolved[:20])
        raise BundlerError(f"Gradle 이 해석하지 못한 좌표가 있습니다:\n  {shown}")
    for line in result.unresolved[:10]:
        log.warning("Gradle 해석 실패(교차 해석): %s", line)
    return result
