"""학생 코드를 Docker 컨테이너에서 실행한다.

학생 코드는 믿을 수 없으므로 네트워크를 끊고 메모리, CPU, 프로세스 수, 실행 시간을 제한한다.
시연용이라 큐 없이 요청 안에서 바로 실행한다. 운영으로 넘어가면 Redis 큐와 별도 실행 서버로 옮긴다.

실행 방식
- run: 지금 파일을 한 번 실행한다. stdin 을 넣을 수 있다.
- test: Python 은 test_*.py 파일의 test_ 함수를 돌린다. 그 밖에는 과제 설정의 tests(입력과 기대 출력)로 채점한다.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, TypedDict

from ..config import DATA_DIR, settings
from ..util import new_token

MAX_OUTPUT = 20_000
COMPILE_FAIL_EXIT = 101

# Python 테스트 실행기. pytest 를 설치하지 않고 test_ 함수를 정의 순서대로 돌린다.
PY_TEST_RUNNER = r'''
import contextlib, importlib, io, json, sys, time, traceback
results = []
try:
    mod = importlib.import_module(sys.argv[1])
except BaseException:
    print("@@IMPORT_ERROR")
    traceback.print_exc()
    sys.exit(2)
for name, fn in list(vars(mod).items()):
    if not name.startswith("test_") or not callable(fn):
        continue
    buf = io.StringIO()
    start = time.perf_counter()
    try:
        with contextlib.redirect_stdout(buf):
            fn()
        ok, message = True, ""
    except BaseException as exc:
        ok = False
        tb = traceback.extract_tb(exc.__traceback__)
        line = tb[-1].line if tb else ""
        message = (type(exc).__name__ + (": " + str(exc) if str(exc) else "") + ("\n" + line if line else "")).strip()
    results.append({"name": name, "passed": ok, "duration": round(time.perf_counter() - start, 3), "message": message, "stdout": buf.getvalue()[:2000]})
print("@@RESULTS " + json.dumps(results, ensure_ascii=False))
'''


class TestResult(TypedDict, total=False):
    name: str
    passed: bool
    duration: float
    message: str
    input: str
    expected: str
    actual: str


class RunUnavailable(Exception):
    """Docker 를 쓸 수 없다."""


def docker_available() -> bool:
    if shutil.which(settings.docker_bin) is None:
        return False
    try:
        r = subprocess.run([settings.docker_bin, "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _image(language: str) -> str:
    return {"python": settings.run_image_python, "cpp": settings.run_image_cpp, "java": settings.run_image_java}[language]


def java_class_name(path: str, source: str) -> str:
    m = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
    return f"{m.group(1)}.{Path(path).stem}" if m else Path(path).stem


def _commands(language: str, path: str, java_class: str = "") -> tuple[str, str]:
    """(컴파일 명령, 실행 명령). 컴파일이 없으면 true."""
    q = shlex.quote
    if language == "python":
        return "true", f"python -u /code/{q(path)}"
    if language == "cpp":
        # 지금 파일만 컴파일한다. 헤더는 파일 폴더, 작업공간 맨 위, include/ 에서 찾는다
        folder = q("/code/" + (str(Path(path).parent) if str(Path(path).parent) != "." else ""))
        return f"g++ -std=c++17 -O2 -I{folder} -I/code -I/code/include -o /tmp/a.out /code/{q(path)}", "/tmp/a.out"
    if language == "java":
        # 하위 폴더까지 모든 .java 를 컴파일한다. 실행할 클래스는 package 선언과 파일 이름으로 정한다
        return (
            "mkdir -p /tmp/out && find /code -name '*.java' -not -path '*/__*' > /tmp/sources && "
            "javac -encoding UTF-8 -d /tmp/out @/tmp/sources",
            f"java -Xmx128m -cp /tmp/out {q(java_class)}",
        )
    raise ValueError(f"지원하지 않는 언어: {language}")


def _cases_script(language: str, path: str, count: int, java_class: str = "") -> str:
    compile_cmd, run_cmd = _commands(language, path, java_class)
    t = settings.run_timeout_seconds
    return f"""
{compile_cmd} > /tmp/compile.log 2>&1 || {{ echo "@@COMPILE_FAIL"; cat /tmp/compile.log; exit {COMPILE_FAIL_EXIT}; }}
i=0
while [ $i -lt {count} ]; do
  start=$(date +%s%N)
  timeout {t} {run_cmd} < /code/__cases__/$i.in > /tmp/__run_output 2>&1
  code=$?
  end=$(date +%s%N)
  echo "@@CASE $i $code $(( (end - start) / 1000000 ))"
  head -c {MAX_OUTPUT} /tmp/__run_output
  echo
  echo "@@END"
  i=$((i + 1))
done
"""


def _docker(job: Path, image: str, script: str, timeout: int) -> tuple[int, str, bool]:
    cmd = [
        settings.docker_bin, "run", "--rm", "-i",
        "--network", "none",
        "--memory", "384m", "--cpus", "1", "--pids-limit", "64",
        "--read-only", "--tmpfs", "/tmp:rw,exec,size=64m",
        "--user", "1000:1000", "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/code",
        "-v", f"{job}:/code:ro", "-w", "/code",
        image, "sh", "-c", script,
    ]  # fmt: skip
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", True
    except OSError as exc:
        raise RunUnavailable(f"Docker 를 실행하지 못했다: {exc}") from exc
    out = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    if r.returncode == 125 or "Cannot connect to the Docker daemon" in out or "failed to connect to the docker API" in out:
        raise RunUnavailable("Docker 가 꺼져 있다. Docker Desktop 을 켠 뒤 다시 실행한다")
    if "Unable to find image" in out and r.returncode != 0:
        raise RunUnavailable(f"실행 이미지 {image} 를 받지 못했다. scripts/pull_run_images.sh 를 먼저 실행한다")
    return r.returncode, out, False


def _parse_cases(out: str) -> tuple[str | None, list[dict[str, Any]]]:
    if "@@COMPILE_FAIL" in out:
        return out.split("@@COMPILE_FAIL", 1)[1].strip()[:MAX_OUTPUT], []
    cases: list[dict[str, Any]] = []
    for chunk in out.split("@@CASE ")[1:]:
        header, _, rest = chunk.partition("\n")
        body = rest.rsplit("@@END", 1)[0]
        parts = header.split()
        cases.append({"code": int(parts[1]), "ms": int(parts[2]) if len(parts) > 2 else 0, "output": body[:-1] if body.endswith("\n") else body})
    return None, cases


def run(files: dict[str, str], language: str, path: str, mode: str, stdin: str, tests: list[dict[str, Any]]) -> dict[str, Any]:
    """files 는 {경로: 내용}. 결과 사전을 돌려준다."""
    job = DATA_DIR / "runs" / new_token(9)
    job.mkdir(parents=True)
    try:
        for p, content in files.items():
            dest = job / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        image = _image(language)
        limit = settings.compile_timeout_seconds + settings.run_timeout_seconds * max(1, len(tests)) + 10
        started = time.perf_counter()
        result: dict[str, Any] = {"mode": mode, "language": language, "path": path, "compile_error": None, "tests": []}

        if mode == "test" and language == "python" and _python_test_module(files, path):
            module = _python_test_module(files, path)
            (job / "__runner__.py").write_text(PY_TEST_RUNNER, encoding="utf-8")
            code, out, timed_out = _docker(job, image, f"timeout {settings.run_timeout_seconds * 4} python -u /code/__runner__.py {shlex.quote(module)}", limit)
            result.update(timed_out=timed_out, exit_code=code)
            if "@@RESULTS " in out:
                head, _, data = out.rpartition("@@RESULTS ")
                result["tests"] = json.loads(data.strip().splitlines()[0])
                result["stdout"] = head[:MAX_OUTPUT]
            else:
                result["stdout"] = out.replace("@@IMPORT_ERROR\n", "")[:MAX_OUTPUT]
                result["error"] = "테스트를 불러오지 못했다" if "@@IMPORT_ERROR" in out else "테스트 실행이 중간에 멈췄다"
        else:
            cases = tests if mode == "test" else [{"name": "실행", "stdin": stdin}]
            if mode == "test" and not cases:
                raise ValueError("이 과제에는 테스트가 없다")
            (job / "__cases__").mkdir()
            for i, c in enumerate(cases):
                (job / "__cases__" / f"{i}.in").write_text(str(c.get("stdin", "")), encoding="utf-8")
            code, out, timed_out = _docker(job, image, _cases_script(language, path, len(cases), java_class_name(path, files.get(path, ''))), limit)
            compile_error, parsed = _parse_cases(out)
            result.update(timed_out=timed_out, exit_code=code, compile_error=compile_error)
            if mode == "run":
                first = parsed[0] if parsed else {"code": code, "output": out if not compile_error else ""}
                result["stdout"] = first["output"]
                result["exit_code"] = first["code"]
                result["timed_out"] = timed_out or first["code"] == 124
            else:
                tr: list[TestResult] = []
                for c, p in zip(cases, parsed, strict=False):
                    expected = str(c.get("expected", "")).strip()
                    actual = p["output"].strip()
                    passed = p["code"] == 0 and actual == expected
                    msg = "" if passed else ("시간 초과" if p["code"] == 124 else f"종료 코드 {p['code']}" if p["code"] != 0 else "출력이 기대값과 다르다")
                    tr.append({"name": str(c.get("name", "")), "passed": passed, "duration": p["ms"] / 1000, "message": msg, "input": str(c.get("stdin", "")), "expected": expected, "actual": actual[:2000]})
                result["tests"] = tr
                result["stdout"] = ""

        result["duration"] = round(time.perf_counter() - started, 2)
        result["passed"] = sum(1 for t in result["tests"] if t.get("passed"))
        result["total"] = len(result["tests"])
        return result
    finally:
        shutil.rmtree(job, ignore_errors=True)


def _python_test_module(files: dict[str, str], path: str) -> str:
    """실행할 테스트 모듈 이름. 지금 파일이 테스트면 그 파일, 아니면 첫 test_*.py."""
    name = Path(path).name
    if name.startswith("test_") and name.endswith(".py"):
        return Path(path).with_suffix("").as_posix().replace("/", ".")
    for p in sorted(files):
        if Path(p).name.startswith("test_") and p.endswith(".py"):
            return Path(p).with_suffix("").as_posix().replace("/", ".")
    return ""
