#!/usr/bin/env python3
"""sneakernet CLI.

  collect : pom.xml / build.gradle / 좌표의 종속성을 하나의 디렉토리로 수집 (인터넷 되는 PC)
  upload  : 수집된 디렉토리를 Nexus 3 maven2 hosted 저장소에 업로드 (폐쇄망)
  verify  : 업로드된 Nexus 만으로 Maven/Gradle 이 해석되는지 확인 (폐쇄망)
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from snk import __version__  # noqa: E402
from snk.common import BundlerError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sneakernet", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"sneakernet {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="자세한 로그 출력")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="종속성 수집")
    c.add_argument("--input", "-i", action="append", metavar="FILE",
                   help="pom.xml 또는 build.gradle[.kts] 파일 (여러 번 지정 가능)")
    c.add_argument("--coordinate", "-c", action="append", metavar="G:A:V",
                   help="단일 종속성 좌표, 예: org.springframework:spring-core:6.1.0 (여러 번 지정 가능)")
    c.add_argument("--out", "-o", required=True, help="결과 디렉토리 (비어 있어야 함)")
    c.add_argument("--platforms", help="대상 플랫폼, 쉼표 구분 (기본: repos.json 의 default_platforms)")
    c.add_argument("--repos-config", help="저장소/플랫폼 설정 파일 (기본: repos.json)")
    c.add_argument("--mvn-cmd", help="mvn 실행 파일 경로")
    c.add_argument("--gradle-cmd", help="gradle 실행 파일 경로")
    c.add_argument("--maven-goals", help="추가로 실행할 Maven 목표, 예: 'verify -DskipTests' (테스트 provider 등 수집용)")
    c.add_argument("--no-cross", action="store_true", help="반대 도구용 교차 해석을 건너뜀 (빠르지만 덜 완전함)")
    c.add_argument("--no-losers", action="store_true", help="충돌로 탈락한 버전의 POM/jar 를 수집하지 않음")
    c.add_argument("--no-proxy", action="store_true", help="환경 변수의 HTTP 프록시를 무시")
    c.add_argument("--workers", type=int, default=8, help="보강 단계 동시 다운로드 수 (기본 8)")
    c.add_argument("--work-dir", help="작업 디렉토리 지정 (지정하면 삭제하지 않고 로그를 남김)")
    c.add_argument("--keep-work", action="store_true", help="작업 디렉토리를 삭제하지 않음")
    c.add_argument("--force", action="store_true", help="출력 디렉토리가 비어 있지 않으면 삭제 후 다시 만듦")

    u = sub.add_parser("upload", help="Nexus 3 에 업로드")
    u.add_argument("--dir", "-d", required=True, help="collect 결과 디렉토리")
    u.add_argument("--url", required=True, help="Nexus 주소, 예: http://nexus:8081")
    u.add_argument("--repo", required=True, help="maven2 hosted 저장소 이름")
    u.add_argument("--user", "-u", help="업로드 계정")
    u.add_argument("--password", "-p", help="비밀번호 (환경 변수 NEXUS_PASSWORD 또는 터미널 입력 권장)")
    u.add_argument("--workers", type=int, default=4, help="동시 업로드 수 (기본 4)")
    u.add_argument("--check-exists", action="store_true", help="올리기 전에 HEAD 로 존재 여부를 확인해 건너뜀")
    u.add_argument("--dry-run", action="store_true", help="대상 파일만 세고 업로드하지 않음")
    u.add_argument("--no-proxy", action="store_true", help="환경 변수의 HTTP 프록시를 무시")
    u.add_argument("--report", default="upload-report.json", help="결과 보고서 경로 (기본: upload-report.json)")

    v = sub.add_parser("verify", help="Nexus 만으로 Maven/Gradle 이 해석되는지 검증")
    v.add_argument("--url", required=True)
    v.add_argument("--repo", required=True)
    v.add_argument("--samples", nargs="+", required=True, metavar="PATH",
                   help="샘플 프로젝트 디렉토리/파일. collect 결과의 <out>/_samples 를 그대로 줄 수 있음")
    v.add_argument("--user", "-u")
    v.add_argument("--password", "-p")
    v.add_argument("--tools", default="maven,gradle", help="검증할 도구 (기본: maven,gradle)")
    v.add_argument("--offline-build", action="store_true", help="Maven 은 해석 후 -o package -DskipTests 까지 수행")
    v.add_argument("--mvn-cmd")
    v.add_argument("--gradle-cmd")
    v.add_argument("--work-dir")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        if args.command == "collect":
            from snk import collect
            return collect.run(args)
        if args.command == "upload":
            from snk import upload
            return upload.run(args)
        from snk import verify
        return verify.run(args)
    except BundlerError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단되었습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
