"""보강: 어느 도구로 수집했든 Maven/Gradle 양쪽이 해석할 수 있도록 부족한 파일을 원격 저장소에서 채운다.

- 모든 GAV 의 POM (parent POM, import 된 BOM 은 재귀적으로)
- Gradle 모듈 메타데이터(.module) 와 거기에 적힌 같은 디렉토리의 파일(플랫폼 필터 적용)
- 대표 아티팩트(jar 등) - Gradle 로 수집하면 빠질 수 있는 Maven 소비자용 파일
- 충돌로 탈락한 버전(POM/jar/.module)
- repos.json 의 native_hints 에 해당하는 플랫폼별 classifier 파일
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.entities import name2codepoint
from pathlib import Path

from .common import GAV, BundlerError, HttpClient, file_matches_platforms, log, sha1_hex

# POM <packaging> → 대표 아티팩트 확장자 (None 이면 파일 없음)
_PACKAGING_EXT = {
    "jar": "jar", "bundle": "jar", "maven-plugin": "jar", "ejb": "jar", "eclipse-plugin": "jar",
    "hk2-jar": "jar", "orbit": "jar", "aar": "aar", "war": "war", "zip": "zip", "pom": None,
}


_ENTITY_RE = re.compile(rb"&([A-Za-z][A-Za-z0-9]*);")
_XML_ENTITIES = {b"amp", b"lt", b"gt", b"quot", b"apos"}


def _html_entity_to_charref(m: re.Match) -> bytes:
    name = m.group(1)
    if name in _XML_ENTITIES:
        return m.group(0)
    cp = name2codepoint.get(name.decode("ascii"))
    return b"&#%d;" % cp if cp else b""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element | None, name: str) -> str:
    c = _child(el, name) if el is not None else None
    return (c.text or "").strip() if c is not None else ""


@dataclass
class PomInfo:
    group: str
    version: str
    packaging: str
    parent: GAV | None
    props: dict[str, str]
    imports: list[tuple[str, str, str]] = field(default_factory=list)


def parse_pom(path: Path) -> PomInfo | None:
    try:
        data = path.read_bytes()
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            # 오래된 POM 은 &oslash; 같은 HTML 엔티티를 쓴다 (Maven 은 관대하게 읽는다).
            root = ET.fromstring(_ENTITY_RE.sub(_html_entity_to_charref, data))
    except (ET.ParseError, OSError) as e:
        log.warning("POM 파싱 실패 %s: %s", path, e)
        return None
    parent_el = _child(root, "parent")
    parent = None
    if parent_el is not None:
        pg, pa, pv = (_text(parent_el, n) for n in ("groupId", "artifactId", "version"))
        if pg and pa and pv:
            parent = GAV(pg, pa, pv)
    group = _text(root, "groupId") or (parent.group if parent else "")
    version = _text(root, "version") or (parent.version if parent else "")
    props = {}
    props_el = _child(root, "properties")
    if props_el is not None:
        props = {_local(c.tag): (c.text or "").strip() for c in props_el}
    imports = []
    dm = _child(root, "dependencyManagement")
    deps = _child(dm, "dependencies") if dm is not None else None
    if deps is not None:
        for d in deps:
            if _text(d, "scope") == "import" and _text(d, "type") == "pom":
                imports.append((_text(d, "groupId"), _text(d, "artifactId"), _text(d, "version")))
    return PomInfo(group=group, version=version, packaging=_text(root, "packaging") or "jar",
                   parent=parent, props=props, imports=imports)


def resolve_props(text: str, props: dict[str, str]) -> str | None:
    for _ in range(6):
        new = re.sub(r"\$\{([^}]+)\}", lambda m: props.get(m.group(1), m.group(0)), text)
        if new == text:
            break
        text = new
    return None if "${" in text else text


def module_files(path: Path, with_sources: bool = False) -> list[str]:
    """Gradle 모듈 메타데이터(.module)에서 같은 디렉토리에 있는 파일명을 모은다."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names: list[str] = []
    for variant in data.get("variants", []):
        if not with_sources and variant.get("attributes", {}).get("org.gradle.category") == "documentation":
            continue
        for f in variant.get("files", []):
            name = f.get("url") or f.get("name") or ""
            if name and "/" not in name and "\\" not in name and name not in names:
                names.append(name)
    return names


class Enricher:
    def __init__(self, stage: Path, repos: list[dict], client: HttpClient, platforms: list[dict],
                 hints: list[dict], origins: dict[str, str], *, with_sources: bool = False, workers: int = 8):
        self.stage = stage
        self.repos = repos
        self.client = client
        self.platforms = platforms
        self.hints = hints
        self.origins = origins
        self.with_sources = with_sources
        self.workers = workers
        self.lock = threading.Lock()
        self.missing: set[str] = set()
        self.seen: set[GAV] = set()
        self.counts: Counter = Counter()
        self.problems: list[str] = []
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ 다운로드
    def _verify_checksum(self, url: str, data: bytes) -> None:
        status, body = self.client.get(url + ".sha1")
        if status != 200:
            return
        tokens = body.decode("ascii", errors="ignore").split()
        expected = tokens[0].lower() if tokens else ""
        if len(expected) == 40 and expected != sha1_hex(data):
            raise BundlerError(f"체크섬 불일치: {url}")

    def fetch(self, rel: str, kind: str) -> bool:
        """stage 에 rel 이 없으면 저장소들에서 받아온다. 이미 있거나 받았으면 True."""
        dest = self.stage / rel
        if dest.exists():
            return True
        with self.lock:
            if rel in self.missing:
                return False
        errors: list[str] = []
        for repo in self.repos:
            url = repo["url"] + rel
            try:
                status, data = self.client.get(url)
                if status == 404:
                    continue
                if status != 200:
                    errors.append(f"HTTP {status} {url}")
                    continue
                self._verify_checksum(url, data)
            except BundlerError as e:
                errors.append(str(e))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(f".{dest.name}.{threading.get_ident()}.part")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            with self.lock:
                self.origins[rel] = f"enrich:{repo['id']}"
                self.counts[kind] += 1
            return True
        with self.lock:
            self.missing.add(rel)
            if errors:
                self.warnings.append(f"{rel}: " + "; ".join(errors))
        return False

    # ------------------------------------------------------------------- 처리
    def _props(self, gav: GAV, info: PomInfo, depth: int = 0) -> dict[str, str]:
        props: dict[str, str] = {}
        if info.parent and depth < 10:
            self.fetch(info.parent.file("pom"), "pom")
            parent_info = parse_pom(self.stage / info.parent.file("pom"))
            if parent_info:
                props.update(self._props(info.parent, parent_info, depth + 1))
        props.update(info.props)
        for prefix in ("project.", "pom.", ""):
            props[f"{prefix}version"] = info.version
            props[f"{prefix}groupId"] = info.group
        props["project.artifactId"] = gav.artifact
        if info.parent:
            props["project.parent.version"] = info.parent.version
            props["project.parent.groupId"] = info.parent.group
        return props

    def process(self, gav: GAV) -> list[GAV]:
        discovered: list[GAV] = []
        pom_rel = gav.file("pom")
        info = None
        if self.fetch(pom_rel, "pom"):
            info = parse_pom(self.stage / pom_rel)
        else:
            with self.lock:
                self.problems.append(f"POM 을 찾을 수 없음: {gav}")
        if info:
            if info.parent:
                discovered.append(info.parent)
            props = self._props(gav, info)
            for g, a, v in info.imports:
                g2, a2, v2 = (resolve_props(x, props) for x in (g, a, v))
                if g2 and a2 and v2:
                    discovered.append(GAV(g2, a2, v2))
                else:
                    log.debug("BOM import 버전을 해석하지 못함: %s:%s:%s (in %s)", g, a, v, gav)

        has_module = self.fetch(gav.file("module"), "module")

        packaging = info.packaging if info else "jar"
        ext = _PACKAGING_EXT.get(packaging)
        if ext:
            self.fetch(gav.file(ext), "artifact")

        if has_module:
            for name in module_files(self.stage / gav.file("module"), self.with_sources):
                if not file_matches_platforms(name, self.platforms):
                    continue
                if not self.with_sources and name.endswith(("-sources.jar", "-javadoc.jar")):
                    continue
                self.fetch(f"{gav.rel_dir}/{name}", "artifact")

        coord = f"{gav.group}:{gav.artifact}"
        for hint in self.hints:
            if fnmatch.fnmatchcase(coord, hint["match"]):
                for p in self.platforms:
                    for classifier in hint.get("classifiers", {}).get(p["key"], []):
                        self.fetch(gav.file(hint.get("extension", "jar"), classifier), "native")
        return discovered

    def _safe_process(self, gav: GAV) -> list[GAV]:
        try:
            return self.process(gav)
        except Exception as e:  # 한 GAV 의 실패가 전체 보강을 멈추지 않게 한다.
            with self.lock:
                self.problems.append(f"보강 중 오류 {gav}: {e}")
            return []

    def run(self, seeds: set[GAV]) -> "Enricher":
        queue = sorted(seeds)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while queue:
                batch = [g for g in queue if g not in self.seen]
                self.seen.update(batch)
                queue = []
                for discovered in pool.map(self._safe_process, batch):
                    queue.extend(discovered)
        return self
