"""정규화: Maven 로컬 저장소와 Gradle 캐시를 하나의 Maven2 레이아웃 디렉토리(stage)로 합친다."""
from __future__ import annotations

import shutil
from pathlib import Path

from .common import GAV, is_junk, log


def _copy(src: Path, dst: Path, origin: str, origins: dict[str, str], stage: Path) -> bool:
    if dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    origins[dst.relative_to(stage).as_posix()] = origin
    return True


def import_maven_local(m2: Path, stage: Path, origins: dict[str, str]) -> tuple[int, set[str]]:
    """Maven 로컬 저장소 → stage. (복사한 파일 수, 건너뛴 SNAPSHOT 디렉토리 목록)"""
    copied, snapshots = 0, set()
    if not m2.is_dir():
        return 0, snapshots
    for src in sorted(m2.rglob("*")):
        if not src.is_file() or is_junk(src.name):
            continue
        rel = src.relative_to(m2)
        if len(rel.parts) < 4:
            continue
        if rel.parts[-2].endswith("-SNAPSHOT"):
            snapshots.add("/".join(rel.parts[:-1]))
            continue
        copied += _copy(src, stage / rel, "maven", origins, stage)
    return copied, snapshots


def import_gradle_cache(ghome: Path, stage: Path, origins: dict[str, str]) -> tuple[int, set[str]]:
    """Gradle 캐시(modules-2/files-2.1/group/artifact/version/해시/파일) → stage(Maven 레이아웃)."""
    copied, snapshots = 0, set()
    root = ghome / "caches" / "modules-2" / "files-2.1"
    if not root.is_dir():
        return 0, snapshots
    for src in sorted(root.rglob("*")):
        if not src.is_file() or is_junk(src.name):
            continue
        rel = src.relative_to(root)
        if len(rel.parts) != 5:  # group, artifact, version, hash, filename
            continue
        group, artifact, version, _hash, filename = rel.parts
        if version.endswith("-SNAPSHOT"):
            snapshots.add(f"{group}/{artifact}/{version}")
            continue
        dst = stage / group.replace(".", "/") / artifact / version / filename
        copied += _copy(src, dst, "gradle", origins, stage)
    return copied, snapshots


def scan_gavs(stage: Path) -> dict[GAV, list[str]]:
    """stage 를 훑어 GAV 별 파일 목록(파일명)을 만든다."""
    result: dict[GAV, list[str]] = {}
    for f in stage.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(stage)
        if len(rel.parts) < 4 or rel.parts[0].startswith("_"):
            continue
        version, artifact = rel.parts[-2], rel.parts[-3]
        group = ".".join(rel.parts[:-3])
        if not f.name.startswith(f"{artifact}-{version}"):
            continue
        result.setdefault(GAV(group, artifact, version), []).append(f.name)
    return result


def remove_junk(stage: Path) -> int:
    removed = 0
    for f in list(stage.rglob("*")):
        if f.is_file() and is_junk(f.name):
            f.unlink()
            removed += 1
    if removed:
        log.debug("잡파일 %d개 제거", removed)
    return removed
