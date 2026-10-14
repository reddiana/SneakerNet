"""pip 을 대상 플랫폼의 환경 마커로 실행한다:  python -m snk.pip_target download ...

`pip download --platform` 은 wheel 만 대상 플랫폼용으로 고르고, `; sys_platform == "win32"` 같은 환경 마커는
수집하는 PC 기준으로 평가한다. 그대로 두면 다른 OS 전용 의존성이 빠지거나, 없는 wheel 을 찾다가 실패한다.
환경 변수 SNK_MARKER_ENV (JSON) 의 값으로 마커 환경을 덮어쓴 뒤 pip 을 실행한다.
"""
import importlib
import json
import os
import sys

target = json.loads(os.environ.get("SNK_MARKER_ENV") or "{}")
patched = False
for name in ("pip._vendor.packaging.markers", "packaging.markers"):
    try:
        module = importlib.import_module(name)
        original = module.default_environment
    except (ImportError, AttributeError):
        continue
    module.default_environment = lambda original=original: {**original(), **target}
    patched = True
if target and not patched:
    print("WARNING pip 의 환경 마커를 바꾸지 못했습니다. 다른 OS 전용 의존성이 빠질 수 있습니다.", file=sys.stderr)

from pip._internal.cli.main import main  # noqa: E402

sys.exit(main(sys.argv[1:]))
