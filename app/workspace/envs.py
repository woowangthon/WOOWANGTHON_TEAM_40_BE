"""실행 환경. 기본 환경과 교수 지정 환경.

- 기본 환경: 언어별 공식 이미지(python:3.12-slim, gcc:14, eclipse-temurin:21-jdk)를 그대로 쓴다.
- 교수 환경: 교수가 고른 OS 프리셋에 Python, g++, JDK 와 추가 apt, pip 패키지를 넣은 이미지 하나를 만든다.
  Dockerfile 은 서버가 만들고(교수가 직접 쓰지 않는다) 같은 설정이면 같은 tag 를 다시 쓴다.
  빌드할 때만 인터넷을 쓰고 실행할 때는 그대로 네트워크를 끊는다.

과제 설정 tool_config["environment"] 모양:
    {"preset": "ubuntu-22.04", "name": "운영체제 과제 채점 환경", "required": false,
     "apt": ["valgrind"], "pip": ["numpy"], "cflags": "-std=c++17 -O2 -Wall"}
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from functools import lru_cache
from typing import Any

from ..config import DATA_DIR, settings
from ..db import SessionLocal
from ..models import EnvBuild
from ..util import utcnow
from .runner import DEFAULT_CFLAGS

PRESETS: dict[str, dict[str, Any]] = {
    "ubuntu-22.04": {
        "os": "Ubuntu 22.04 LTS",
        "from": "ubuntu:22.04",
        "python": "Python 3.10",
        "cpp": "g++ 11",
        "java": "OpenJDK 17",
        "apt": ["python3", "python3-pip", "g++", "openjdk-17-jdk-headless"],
    },
    "ubuntu-24.04": {
        "os": "Ubuntu 24.04 LTS",
        "from": "ubuntu:24.04",
        "python": "Python 3.12",
        "cpp": "g++ 13",
        "java": "OpenJDK 21",
        "apt": ["python3", "python3-pip", "g++", "openjdk-21-jdk-headless"],
    },
    "debian-12": {
        "os": "Debian 12 (bookworm)",
        "from": "debian:12",
        "python": "Python 3.11",
        "cpp": "g++ 12",
        "java": "OpenJDK 17",
        "apt": ["python3", "python3-pip", "g++", "openjdk-17-jdk-headless"],
    },
}

DEFAULT_ENV: dict[str, Any] = {
    "key": "default",
    "name": "서비스 기본 환경",
    "os": "Debian 12 (bookworm)",
    "python": "Python 3.12",
    "cpp": "g++ 14",
    "java": "OpenJDK 21",
    "apt": [],
    "pip": [],
    "cflags": DEFAULT_CFLAGS,
    "status": "ready",
}

APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]{0,63}$")
PIP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,63}(\[[A-Za-z0-9,_\-]+\])?((==|>=|<=|~=)[A-Za-z0-9.*]+)?$")
FLAG_RE = re.compile(r"^-[A-Za-z0-9=+_.,:\-]{1,40}$")


class EnvError(ValueError):
    """교수 환경 설정이 올바르지 않다."""


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """교수가 넣은 값을 검사하고 정리한다. 이름에 셸 문자가 들어가지 못하게 정해진 모양만 받는다."""
    preset = str(raw.get("preset", ""))
    if preset not in PRESETS:
        raise EnvError(f"지원하지 않는 OS 다. 고를 수 있는 값: {', '.join(PRESETS)}")
    apt = [str(x).strip() for x in raw.get("apt", []) if str(x).strip()]
    pip = [str(x).strip() for x in raw.get("pip", []) if str(x).strip()]
    bad = [x for x in apt if not APT_RE.match(x)] + [x for x in pip if not PIP_RE.match(x)]
    if bad:
        raise EnvError("패키지 이름이 올바르지 않다: " + ", ".join(bad))
    if len(apt) > 20 or len(pip) > 20:
        raise EnvError("추가 패키지는 각각 20개까지다")
    flags = str(raw.get("cflags") or DEFAULT_CFLAGS).split()
    if not flags or any(not FLAG_RE.match(f) for f in flags):
        raise EnvError("컴파일 옵션은 -std=c++17 처럼 - 로 시작하는 값만 넣는다")
    return {
        "preset": preset,
        "name": str(raw.get("name") or "교수 지정 환경")[:60],
        "required": bool(raw.get("required", False)),
        "apt": apt,
        "pip": pip,
        "cflags": " ".join(flags),
    }


def dockerfile(spec: dict[str, Any]) -> str:
    p = PRESETS[spec["preset"]]
    apt = " ".join(p["apt"] + spec["apt"])
    lines = [
        f"FROM {p['from']}",
        "ENV DEBIAN_FRONTEND=noninteractive PIP_BREAK_SYSTEM_PACKAGES=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1",
        f"RUN apt-get update && apt-get install -y --no-install-recommends {apt} "
        "&& rm -rf /var/lib/apt/lists/* && ln -sf /usr/bin/python3 /usr/local/bin/python",
    ]
    if spec["pip"]:
        lines.append("RUN python3 -m pip install " + " ".join(spec["pip"]))
    return "\n".join(lines) + "\n"


def image_tag(spec: dict[str, Any]) -> str:
    digest = hashlib.sha256(dockerfile(spec).encode()).hexdigest()[:12]
    return f"woowang-env:{digest}"


def professor_spec(tool_config: dict[str, Any]) -> dict[str, Any] | None:
    raw = (tool_config or {}).get("environment")
    if not raw:
        return None
    try:
        return normalize(raw)
    except EnvError:
        return None


def describe(spec: dict[str, Any]) -> dict[str, Any]:
    """화면에 보여 줄 모양. 빌드 상태를 함께 넣는다."""
    p = PRESETS[spec["preset"]]
    tag = image_tag(spec)
    return {
        "key": "professor",
        "name": spec["name"],
        "required": spec["required"],
        "os": p["os"],
        "python": p["python"],
        "cpp": p["cpp"],
        "java": p["java"],
        "apt": spec["apt"],
        "pip": spec["pip"],
        "cflags": spec["cflags"],
        "tag": tag,
        **build_status(tag),
    }


# ---------- Docker 이미지 빌드 ----------
_lock = threading.Lock()
_building: set[str] = set()


@lru_cache(maxsize=1)
def _docker_env() -> dict[str, str]:
    """빌드는 공개 이미지만 받는다. 로그인 도우미 설정이 꼬인 컴퓨터에서도 받을 수 있게 빈 설정 폴더를 쓰고,
    지금 쓰는 Docker 엔진 주소는 현재 context 에서 그대로 읽어 온다."""
    import os

    cfg = DATA_DIR / "docker-config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.json").write_text("{}", encoding="utf-8")
    env = {**os.environ, "DOCKER_CONFIG": str(cfg)}
    if "DOCKER_HOST" not in env:
        try:
            r = subprocess.run(
                [settings.docker_bin, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
                capture_output=True, text=True, timeout=10,
            )  # fmt: skip
            host = r.stdout.strip()
            if r.returncode == 0 and host:
                env["DOCKER_HOST"] = host
        except (OSError, subprocess.TimeoutExpired):
            pass
    return env


def image_exists(tag: str) -> bool:
    try:
        r = subprocess.run([settings.docker_bin, "image", "inspect", tag], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def build_status(tag: str) -> dict[str, Any]:
    with SessionLocal() as db:
        row = db.query(EnvBuild).filter(EnvBuild.tag == tag).first()
        if row is not None and row.status == "ready" and not image_exists(tag):
            # 이미지가 지워졌다. 다시 만들어야 한다
            row.status = "missing"
            db.commit()
        if row is None and image_exists(tag):
            return {"status": "ready", "log": "", "started_at": None, "finished_at": None}
        if row is None:
            return {"status": "none", "log": "", "started_at": None, "finished_at": None}
        return {
            "status": row.status,
            "log": row.log[-1500:],
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        }


def start_build(spec: dict[str, Any]) -> str:
    """이미지가 없으면 백그라운드에서 만든다. tag 를 돌려준다."""
    tag = image_tag(spec)
    with _lock:
        if tag in _building:
            return tag
        if build_status(tag)["status"] == "ready":
            return tag
        _building.add(tag)
    with SessionLocal() as db:
        row = db.query(EnvBuild).filter(EnvBuild.tag == tag).first()
        if row is None:
            row = EnvBuild(tag=tag)
            db.add(row)
        row.status, row.log, row.started_at, row.finished_at = "building", "", utcnow(), None
        db.commit()
    threading.Thread(target=_build, args=(tag, dockerfile(spec)), daemon=True).start()
    return tag


def _build(tag: str, text: str) -> None:
    log: list[str] = []
    status = "failed"
    try:
        proc = subprocess.Popen(
            [settings.docker_bin, "build", "-t", tag, "--label", "woowang.env=1", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=_docker_env(),
        )  # fmt: skip
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(text)
        proc.stdin.close()
        last_save = 0.0
        import time

        for line in proc.stdout:
            log.append(line.rstrip())
            if time.time() - last_save > 2:
                _save(tag, "building", log)
                last_save = time.time()
        proc.wait(timeout=60 * 20)
        status = "ready" if proc.returncode == 0 else "failed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.append(f"빌드를 실행하지 못했다: {exc}")
    finally:
        _save(tag, status, log, done=True)
        with _lock:
            _building.discard(tag)


def _save(tag: str, status: str, log: list[str], done: bool = False) -> None:
    with SessionLocal() as db:
        row = db.query(EnvBuild).filter(EnvBuild.tag == tag).first()
        if row is None:
            return
        row.status = status
        row.log = "\n".join(log[-200:])
        if done:
            row.finished_at = utcnow()
        db.commit()


def spec_json(spec: dict[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, sort_keys=True)


# ---------- 학생이 고른 환경 ----------
def chosen_key(db: Any, enrollment: Any) -> str | None:
    """마지막 env_select 이벤트의 값. 따로 테이블을 두지 않고 과정 기록에서 읽는다."""
    from ..models import TaskAttempt, TaskEvent

    row = (
        db.query(TaskEvent)
        .join(TaskAttempt, TaskAttempt.id == TaskEvent.attempt_id)
        .filter(TaskAttempt.enrollment_id == enrollment.id, TaskEvent.kind == "env_select")
        .order_by(TaskEvent.id.desc())
        .first()
    )
    return str(row.payload.get("env")) if row is not None else None


def env_info(db: Any, enrollment: Any) -> dict[str, Any]:
    spec = professor_spec(enrollment.assignment.tool_config or {})
    prof = describe(spec) if spec else None
    chosen = chosen_key(db, enrollment)
    if prof is None:
        chosen = "default"
    elif prof["required"]:
        chosen = "professor" if chosen else None
    return {"default": DEFAULT_ENV, "professor": prof, "chosen": chosen}


def resolve(db: Any, enrollment: Any, override: str | None = None) -> tuple[str, str | None, str | None, str]:
    """실행에 쓸 (환경 키, 이미지, 컴파일 옵션, 이름). 교수 환경 이미지가 아직 없으면 EnvError."""
    spec = professor_spec(enrollment.assignment.tool_config or {})
    key = override or chosen_key(db, enrollment) or "default"
    if spec and spec["required"]:
        key = "professor"
    if key != "professor" or spec is None:
        return "default", None, None, DEFAULT_ENV["os"]
    tag = image_tag(spec)
    st = build_status(tag)["status"]
    if st != "ready":
        start_build(spec)
        raise EnvError("교수 환경을 준비하는 중이다. 처음 한 번은 몇 분 걸린다")
    return "professor", tag, spec["cflags"], PRESETS[spec["preset"]]["os"]
