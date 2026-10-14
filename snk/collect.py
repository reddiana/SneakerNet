"""collect 서브커맨드: 입력 해석 → 수집 → 정규화 → 교차 해석 → 보강 → 반입용 디렉토리 출력."""
from __future__ import annotations

import csv
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import gradle, maven, normalize
from .common import (GAV, BundlerError, HttpClient, find_tool, host_platform_key, load_config, log,
                     select_platforms)
from .enrich import Enricher


# ----------------------------------------------------------------------- 입력 처리

def classify_input(path: Path) -> str:
    name = path.name.lower()
    if name.endswith((".gradle", ".gradle.kts")):
        return "gradle"
    if name.endswith((".xml", ".pom")):
        return "maven"
    raise BundlerError(f"입력 파일 형식을 알 수 없습니다 (pom.xml 또는 build.gradle[.kts] 이어야 함): {path}")


def input_warnings(path: Path, kind: str, text: str) -> list[str]:
    """단독 파일 해석이 실패하거나 불완전해질 수 있는 패턴을 미리 알려준다."""
    warns = []
    if kind == "maven":
        if re.search(r"<relativePath>\s*[^<\s][^<]*</relativePath>", text):
            warns.append(f"{path.name}: <relativePath> 로 로컬 parent POM 을 참조합니다. 파일 하나만 입력하면 "
                         "parent 는 원격 저장소에서만 찾으므로 해석이 실패할 수 있습니다.")
        if "<modules>" in text:
            warns.append(f"{path.name}: 멀티모듈 pom 입니다. 하위 모듈은 수집되지 않습니다.")
    else:
        if re.search(r"\blibs\.[A-Za-z]", text) or "versionCatalogs" in text:
            warns.append(f"{path.name}: 버전 카탈로그(libs.versions.toml)를 사용합니다. "
                         "같은 위치의 gradle/libs.versions.toml 이 있으면 함께 복사해서 사용합니다.")
        if re.search(r"^\s*apply\s+from\s*:", text, re.M) or "rootProject.file(" in text:
            warns.append(f"{path.name}: 다른 파일을 참조하는 스크립트(apply from 등)입니다. 해석이 실패할 수 있습니다.")
    return warns


def prepare_maven_project(src: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    pom = dest / "pom.xml"
    shutil.copy2(src, pom)
    return pom


def prepare_gradle_project(src: Path, dest: Path, warnings: list[str]) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    kts = src.name.lower().endswith(".kts")
    shutil.copy2(src, dest / ("build.gradle.kts" if kts else "build.gradle"))
    src_dir = src.parent
    settings_src = next((src_dir / n for n in ("settings.gradle", "settings.gradle.kts") if (src_dir / n).exists()), None)
    if settings_src and not re.search(r"\binclude\b", settings_src.read_text(encoding="utf-8", errors="replace")):
        shutil.copy2(settings_src, dest / settings_src.name)
    else:
        if settings_src:
            warnings.append(f"{settings_src.name} 에 하위 프로젝트(include)가 있어 사용하지 않고 단일 프로젝트로 해석합니다.")
        (dest / ("settings.gradle.kts" if kts else "settings.gradle")).write_text(
            "rootProject.name = 'bundler-input'\n" if not kts else 'rootProject.name = "bundler-input"\n',
            encoding="utf-8")
    if (src_dir / "gradle.properties").exists():
        shutil.copy2(src_dir / "gradle.properties", dest / "gradle.properties")
    gdir = src_dir / "gradle"
    if gdir.is_dir():
        for toml in gdir.glob("*.toml"):
            (dest / "gradle").mkdir(exist_ok=True)
            shutil.copy2(toml, dest / "gradle" / toml.name)
    return dest


def _copy_sample(proj: Path, out: Path, name: str) -> None:
    target = out / "_samples" / name
    shutil.copytree(proj, target, ignore=shutil.ignore_patterns(".gradle", "build", "target"), dirs_exist_ok=True)


# ------------------------------------------------------------------------- 본 처리

def _tool(name: str, override: str | None, required: bool) -> str | None:
    try:
        return find_tool(name, override)
    except BundlerError:
        if required:
            raise
        return None


def describe_missing(coords: list[GAV], repos: list[dict], client: HttpClient, source: str = "") -> list[str]:
    """저장소에 없는 것이 확실한(모든 저장소가 404) 좌표마다 사람이 읽을 설명을 돌려준다."""
    missing = []
    for gav in coords:
        try:
            codes = [client.head(repo["url"] + gav.file("pom")) for repo in repos]
        except BundlerError:
            return []   # 저장소에 닿지 못하면 판단하지 않는다
        if any(c != 404 for c in codes):
            continue
        # 빌드 파일에서 이 좌표가 적힌 곳을 알려 준다 (artifact 이름이 있는 줄, 가능하면 버전도 가까이 있는 줄)
        lines = source.splitlines()
        hits = [i for i, line in enumerate(lines) if re.search(rf"(?<![\w.\-]){re.escape(gav.artifact)}(?![\w.\-])", line)]
        near = [i for i in hits if gav.version in "\n".join(lines[i:i + 3])]
        where = f" ({(near or hits)[0] + 1}번째 줄)" if hits else ""
        versions: list[str] = []
        for repo in repos:
            try:
                code, body = client.get(f"{repo['url']}{gav.group.replace('.', '/')}/{gav.artifact}/maven-metadata.xml")
            except BundlerError:
                continue
            if code == 200:
                versions += re.findall(r"<version>\s*([^<\s]+)\s*</version>", body.decode("utf-8", errors="replace"))
        if versions:
            recent = list(dict.fromkeys(versions))[-8:]   # 메타데이터에 적힌 순서(출시 순)
            missing.append(f"오류: 버전 {gav.version} 은(는) 없습니다: {gav}{where}\n"
                           f"오류:   {gav.group}:{gav.artifact} 의 최근 버전: {', '.join(recent)}")
        else:
            missing.append(f"오류: 이런 라이브러리는 없습니다: {gav}{where}\n"
                           f"오류:   group 과 artifact 의 철자를 확인하세요.")
    if missing:
        missing.append("오류: 찾아본 저장소: " + ", ".join(r["id"] for r in repos))
    return missing


def check_coordinates(coords: list[GAV], repos: list[dict], client: HttpClient) -> None:
    """직접 적은 좌표가 저장소에 있는지 먼저 확인하고, 없으면 바로 오류로 끝낸다."""
    missing = describe_missing(coords, repos, client)
    if missing:
        raise BundlerError("저장소에 없는 좌표가 있어 다운로드를 시작하지 않았습니다.\n" + "\n".join(missing))


# Maven / Gradle 이 "찾지 못했다"고 남기는 문장들. 프로젝트 자신의 좌표나 플러그인 goal 은 잡지 않도록 문장별로 맞춘다.
_NOT_FOUND_RES = [re.compile(p) for p in (
    r"Could not find artifact ([\w.\-]+(?::[\w.\-]+){2,4})",
    r"([\w.\-]+(?::[\w.\-]+){2,4}) \(absent\)",
    r"([\w.\-]+(?::[\w.\-]+){2,4}) was not found in ",
    r"Plugin ([\w.\-]+(?::[\w.\-]+){2}) or one of its dependencies could not be resolved",
    r"Could not find ([\w.\-]+:[\w.\-]+:[\w.\-]+)",
)]


def not_found_gavs(text: str) -> list[GAV]:
    """Maven/Gradle 출력에서 찾지 못했다는 좌표를 뽑는다 (group:artifact[:type[:classifier]]:version)."""
    found: list[GAV] = []
    for rx in _NOT_FOUND_RES:
        for token in rx.findall(text):
            parts = token.rstrip(".").split(":")
            gav = GAV(parts[0], parts[1], parts[-1])
            if gav not in found:
                found.append(gav)
    return found


def explain_failure(error: BundlerError, path: Path, logfile: Path, repos: list[dict], client: HttpClient) -> BundlerError:
    """빌드 파일 해석 실패가 저장소에 없는 좌표 때문이면 그 사실을 앞세운 오류로 바꾼다."""
    try:
        log_text = logfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        log_text = ""
    source = path.read_text(encoding="utf-8", errors="replace")
    suspects = not_found_gavs(f"{error}\n{log_text}")
    # Maven 은 첫 번째 실패만 알려 주므로, 파일에 버전까지 적힌 좌표도 함께 확인해 한 번에 모두 보여 준다.
    suspects += [g for g in pom_literal_gavs(source) if g not in suspects]
    missing = describe_missing(suspects, repos, client, source)
    if not missing:
        return error
    if any("없습니다:" in m and "번째 줄)" not in m.splitlines()[0] for m in missing):
        missing.append("오류: 줄 번호가 없는 좌표는 이 파일에 직접 적힌 것이 아니라 다른 라이브러리가 요구하는 것입니다.")
    return BundlerError(f"{path.name} 이(가) 요구하는 좌표가 저장소에 없습니다: {path}\n" + "\n".join(missing)
                        + f"\n오류: 전체 로그: {logfile}")


def pom_literal_gavs(source: str) -> list[GAV]:
    """pom.xml 본문에 groupId/artifactId/version 이 나란히 적힌 좌표 (버전의 ${속성} 은 같은 파일의 값으로 바꾼다)."""
    try:
        root = ET.fromstring(source.encode("utf-8"))
    except ET.ParseError:
        return []
    name = lambda el: el.tag.rsplit("}", 1)[-1]   # noqa: E731
    props = {name(p): (p.text or "").strip() for el in root if name(el) == "properties" for p in el}
    found: list[GAV] = []
    for el in root.iter():   # 프로젝트 자신의 좌표는 빼고 parent / dependency / plugin / extension 만 본다
        if name(el) not in ("parent", "dependency", "plugin", "extension"):
            continue
        part = {name(c): (c.text or "").strip() for c in el}
        g = part.get("groupId") or ("org.apache.maven.plugins" if name(el) == "plugin" else "")
        a = part.get("artifactId", "")
        v = re.sub(r"\$\{([\w.\-]+)\}", lambda m: props.get(m.group(1), m.group(0)), part.get("version", ""))
        if g and a and v and "${" not in f"{g}{a}{v}" and not v.endswith("-SNAPSHOT") and not re.search(r"[\[\(,]", v):
            if GAV(g, a, v) not in found:
                found.append(GAV(g, a, v))
    return found


def run(args) -> int:
    cfg = load_config(args.repos_config)
    repos = cfg["repositories"]
    platforms = select_platforms(cfg, args.platforms)
    host = host_platform_key()

    inputs = [Path(p).resolve() for p in (args.input or [])]
    coords = [GAV.parse(c) for c in (args.coordinate or [])]
    if not inputs and not coords:
        raise BundlerError("--input (pom.xml / build.gradle) 또는 --coordinate (group:artifact:version) 가 필요합니다.")
    for p in inputs:
        if not p.is_file():
            raise BundlerError(f"입력 파일이 없습니다: {p}")
    checker = HttpClient(no_proxy=args.no_proxy, timeout=20, retries=1)
    check_coordinates(coords, repos, checker)

    out = Path(args.out).resolve()
    if out.exists() and any(out.iterdir()):
        if not args.force:
            raise BundlerError(f"출력 디렉토리가 비어 있지 않습니다: {out}  (--force 로 삭제 후 다시 만들 수 있습니다)")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    keep_work = bool(args.keep_work or args.work_dir)
    work = Path(args.work_dir).resolve() if args.work_dir else Path(tempfile.mkdtemp(prefix="snk-"))
    if args.work_dir and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    logdir = work / "logs"
    stage = work / "stage"
    stage.mkdir()

    kinds = [classify_input(p) for p in inputs]
    cross = not args.no_cross
    mvn = _tool("mvn", args.mvn_cmd, required="maven" in kinds)
    grd = _tool("gradle", args.gradle_cmd, required="gradle" in kinds)
    if coords and not (mvn or grd):
        raise BundlerError("--coordinate 는 mvn 또는 gradle 이 필요합니다.")

    warnings: list[str] = []
    origins: dict[str, str] = {}
    snapshots: set[str] = set()
    maven_losers: set[GAV] = set()
    gradle_requested: set[GAV] = set()
    gradle_selected: set[GAV] = set()
    roots_for_gradle: set[GAV] = set()   # Maven 입력 → Gradle 소비자를 위해 Gradle 로 교차 해석
    roots_for_maven: set[GAV] = set()    # Gradle 입력 → Maven 소비자를 위해 Maven 으로 교차 해석

    def import_all() -> None:
        for ghome_or_m2, fn in ((work / "m2", normalize.import_maven_local),
                                (work / "gradle-home", normalize.import_gradle_cache)):
            _, snaps = fn(ghome_or_m2, stage, origins)
            snapshots.update(snaps)

    # ---- 1) 입력 파일별 1차 해석 (입력한 도구로, 플랫폼마다 한 번씩)
    for idx, (path, kind) in enumerate(zip(inputs, kinds)):
        text = path.read_text(encoding="utf-8", errors="replace")
        warnings.extend(input_warnings(path, kind, text))
        proj = work / "projects" / str(idx)
        if kind == "maven":
            pom = prepare_maven_project(path, proj)
            _copy_sample(proj, out, f"{idx:02d}-{path.parent.name or 'maven'}-maven")
            for plat in platforms:
                try:
                    res = maven.resolve_project(pom, work, repos, plat, host, mvn, args.maven_goals, logdir)
                except BundlerError as e:
                    raise explain_failure(e, path, logdir / f"maven-{plat['key']}.log", repos, checker) from None
                maven_losers |= res.losers
                roots_for_gradle |= res.direct | res.losers
        else:
            prepare_gradle_project(path, proj, warnings)
            _copy_sample(proj, out, f"{idx:02d}-{path.parent.name or 'gradle'}-gradle")
            for plat in platforms:
                try:
                    gres = gradle.resolve_project(proj, work, repos, plat, host, grd, logdir)
                except BundlerError as e:
                    raise explain_failure(e, path, logdir / f"gradle-project-{plat['key']}.log", repos, checker) from None
                gradle_requested |= gres.requested
                gradle_selected |= gres.selected
                roots_for_maven |= gres.direct
    import_all()

    # ---- 2) 단일 좌표: 두 도구가 각자 해석
    if coords:
        sample = out / "_samples" / "coordinates"
        sample.mkdir(parents=True, exist_ok=True)
        (sample / "pom.xml").write_text(maven.synthetic_pom(coords), encoding="utf-8")
        (sample / "build.gradle").write_text(gradle.single_build(coords[0]) if len(coords) == 1 else
                                             "plugins { id 'java-library' }\nrepositories { mavenCentral() }\ndependencies {\n"
                                             + "".join(f"    implementation '{c}'\n" for c in coords) + "}\n",
                                             encoding="utf-8")
        (sample / "settings.gradle").write_text("rootProject.name = 'coordinates'\n", encoding="utf-8")
        if not (mvn and grd):
            warnings.append("mvn 또는 gradle 이 없어 좌표를 한 가지 도구로만 해석했습니다. 반대 도구용 메타데이터는 보강 단계에서만 채워집니다.")
        for plat in platforms:
            if mvn:
                maven.resolve_coordinates(coords, work, repos, plat, host, mvn, logdir, f"coords-{plat['key']}", strict=True)
            if grd:
                gradle.resolve_coordinates(coords, work, repos, plat, host, grd, logdir, f"coords-{plat['key']}", strict=True)
        import_all()

    # ---- 3) 교차 해석: 입력하지 않은 쪽 도구로 직접 의존성과 탈락 버전을 해석
    if cross:
        gradle_losers = gradle_requested - gradle_selected
        roots_for_maven |= gradle_losers
        have = normalize.scan_gavs(stage)
        if roots_for_gradle:
            if grd:
                gradle.resolve_coordinates(sorted(roots_for_gradle), work, repos, None, host, grd, logdir, "cross")
            else:
                warnings.append("gradle 이 없어 Gradle 소비자용 교차 해석을 건너뛰었습니다.")
        if roots_for_maven:
            if mvn:
                pom_only = {g for g in roots_for_maven
                            if g in have and not any(f.endswith((".jar", ".aar", ".war")) for f in have[g])}
                maven.resolve_coordinates(sorted(roots_for_maven), work, repos, None, host, mvn, logdir, "cross",
                                          pom_only=pom_only)
            else:
                warnings.append("mvn 이 없어 Maven 소비자용 교차 해석을 건너뛰었습니다.")
        import_all()
    gradle_losers_all = gradle_requested - gradle_selected

    normalize.remove_junk(stage)

    # ---- 4) 보강: POM/parent/BOM, .module, 대표 아티팩트, 네이티브, 탈락 버전
    seeds = set(normalize.scan_gavs(stage))
    if not args.no_losers:
        seeds |= maven_losers | gradle_losers_all
    seeds = {g for g in seeds if not g.version.endswith("-SNAPSHOT")}
    client = HttpClient(no_proxy=args.no_proxy)
    log.info("보강 시작: GAV %d개", len(seeds))
    enricher = Enricher(stage, repos, client, platforms, cfg["native_hints"], origins,
                        with_sources=False, workers=args.workers).run(seeds)

    # ---- 5) 출력
    normalize.remove_junk(stage)
    shutil.copytree(stage, out, dirs_exist_ok=True)
    rows = []
    for f in sorted(stage.rglob("*")):
        if f.is_file():
            rel = f.relative_to(stage).as_posix()
            rows.append((rel, f.stat().st_size, origins.get(rel, "resolver")))
    with open(out / "_artifacts.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "size", "origin"])
        w.writerows(rows)

    if snapshots:
        warnings.append(f"SNAPSHOT 버전 {len(snapshots)}개는 반입 대상에서 제외했습니다: " + ", ".join(sorted(snapshots)[:5]))
    gavs = normalize.scan_gavs(out)
    total = sum(r[1] for r in rows)
    report = [
        f"플랫폼: {', '.join(p['key'] for p in platforms)}",
        f"아티팩트(GAV) {len(gavs)}개, 파일 {len(rows)}개, {total / 1024 / 1024:.1f} MiB",
        "보강으로 추가: " + (", ".join(f"{k} {v}" for k, v in sorted(enricher.counts.items())) or "없음"),
    ]
    report += [f"[경고] {w}" for w in warnings]
    report += [f"[경고] {w}" for w in enricher.warnings[:50]]
    report += [f"[문제] {p}" for p in enricher.problems]
    (out / "_report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    print("\n".join(report))
    print(f"\n출력 디렉토리: {out}")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"작업 디렉토리(로그 포함): {work}")
    if enricher.problems:
        print(f"\n문제 {len(enricher.problems)}건이 있습니다. 위 [문제] 항목을 확인하세요. (종료 코드 2)")
        return 2
    return 0
