"""실행 환경: 교수 환경 설정 검사, Dockerfile, 학생 선택, 실행 이미지 결정."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Assignment
from app.workspace import envs, runner
from tests.test_workspace import _setup

SPEC = {"preset": "ubuntu-22.04", "name": "채점 환경", "apt": ["valgrind"], "pip": ["numpy==2.1.0"], "cflags": "-std=c++17 -O2 -Wall"}


def test_normalize_and_dockerfile() -> None:
    s = envs.normalize(SPEC)
    text = envs.dockerfile(s)
    assert "FROM ubuntu:22.04" in text and "valgrind" in text and "pip install numpy==2.1.0" in text
    assert envs.image_tag(s) == envs.image_tag(envs.normalize(dict(SPEC)))  # 같은 설정이면 같은 이미지
    assert envs.image_tag(s) != envs.image_tag(envs.normalize({**SPEC, "apt": []}))
    # 셸 문자나 이상한 값은 받지 않는다
    for bad in ({**SPEC, "preset": "arch"}, {**SPEC, "apt": ["vim; rm -rf /"]}, {**SPEC, "pip": ["numpy && curl x"]}, {**SPEC, "cflags": "-O2 $(id)"}):
        with pytest.raises(envs.EnvError):
            envs.normalize(bad)


def _login(client: TestClient, email: str) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_env_flow(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    h, eid = _setup(client)
    prof = _login(client, "p@example.com")
    built: list[str] = []
    ready: set[str] = set()
    monkeypatch.setattr(envs, "start_build", lambda spec: built.append(envs.image_tag(spec)) or envs.image_tag(spec))
    monkeypatch.setattr(envs, "build_status", lambda tag: {"status": "ready" if tag in ready else "building", "log": "", "started_at": None, "finished_at": None})
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        runner, "run",
        lambda files, language, path, mode, stdin, tests, image=None, cflags=None: calls.append({"image": image, "cflags": cflags})
        or {"mode": mode, "language": language, "path": path, "compile_error": None, "tests": [], "passed": 0, "total": 0, "stdout": "", "exit_code": 0},
    )  # fmt: skip

    # 교수 환경이 없으면 기본 환경이 정해져 있다
    w = client.get(f"/api/enrollments/{eid}/workspace", headers=h).json()
    assert w["environment"]["professor"] is None and w["environment"]["chosen"] == "default"

    # 교수가 환경을 정하면 빌드가 시작된다
    with SessionLocal() as db:
        aid = db.query(Assignment).first().id  # type: ignore[union-attr]
    r = client.put(f"/api/assignments/{aid}/environment", json=SPEC, headers=prof)
    assert r.status_code == 200, r.text
    assert built and r.json()["environment"]["os"] == "Ubuntu 22.04 LTS"
    assert client.put(f"/api/assignments/{aid}/environment", json={**SPEC, "apt": ["a;b"]}, headers=prof).status_code == 400
    assert client.put(f"/api/assignments/{aid}/environment", json=SPEC, headers=h).status_code == 403  # 학생은 못 바꾼다

    env = client.get(f"/api/enrollments/{eid}/workspace/environment", headers=h).json()
    assert env["chosen"] is None and env["professor"]["status"] == "building"

    # 교수 환경을 고르고 이미지가 아직 없으면 실행을 막는다
    client.post(f"/api/enrollments/{eid}/workspace/environment", json={"key": "professor"}, headers=h)
    r = client.post(f"/api/enrollments/{eid}/workspace/run", json={"path": "main.py", "mode": "run"}, headers=h)
    assert r.status_code == 409 and "준비하는 중" in r.json()["detail"]
    # 이미지가 준비되면 그 이미지와 교수의 컴파일 옵션으로 돌린다
    ready.add(built[-1])
    r = client.post(f"/api/enrollments/{eid}/workspace/run", json={"path": "main.py", "mode": "run"}, headers=h)
    assert r.status_code == 200, r.text
    assert calls[-1] == {"image": built[-1], "cflags": "-std=c++17 -O2 -Wall"}
    assert r.json()["result"]["env"] == "professor"

    # 기본 환경으로 바꾸면 기본 이미지로 돌리고, 제출 전 확인은 교수 환경으로 한다
    client.post(f"/api/enrollments/{eid}/workspace/environment", json={"key": "default"}, headers=h)
    client.post(f"/api/enrollments/{eid}/workspace/run", json={"path": "main.py", "mode": "run"}, headers=h)
    assert calls[-1]["image"] is None
    r = client.post(f"/api/enrollments/{eid}/workspace/run", json={"path": "main.py", "mode": "run", "env": "professor"}, headers=h)
    assert calls[-1]["image"] == built[-1] and r.json()["commit"]["title"].startswith("교수 환경 확인")

    # 필수로 바꾸면 기본 환경을 고를 수 없고, 고른 값과 상관없이 교수 환경으로 돌린다
    client.put(f"/api/assignments/{aid}/environment", json={**SPEC, "required": True}, headers=prof)
    ready.add(built[-1])
    assert client.post(f"/api/enrollments/{eid}/workspace/environment", json={"key": "default"}, headers=h).status_code == 409
    client.post(f"/api/enrollments/{eid}/workspace/run", json={"path": "main.py", "mode": "run"}, headers=h)
    assert calls[-1]["image"] == built[-1]
