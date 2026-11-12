"""실행 환경 API. 학생은 기본 환경과 교수 환경 중에서 고르고, 교수는 과제의 환경을 정한다."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from ..deps import DB, MyEnrollment, OwnedAssignment
from ..models import TaskEvent
from ..workspace import envs
from .tasks import require_open_attempt

router = APIRouter(tags=["environment"])


class EnvPick(BaseModel):
    key: Literal["default", "professor"]


@router.get("/api/enrollments/{enrollment_id}/workspace/environment")
def get_env(e: MyEnrollment, db: DB) -> dict[str, Any]:
    return envs.env_info(db, e)


@router.post("/api/enrollments/{enrollment_id}/workspace/environment")
def pick_env(body: EnvPick, e: MyEnrollment, db: DB) -> dict[str, Any]:
    attempt = require_open_attempt(e)
    spec = envs.professor_spec(e.assignment.tool_config or {})
    if body.key == "professor" and spec is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "이 과제에는 교수 환경이 없다")
    if body.key == "default" and spec is not None and spec["required"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "교수가 지정 환경을 필수로 정했다")
    os_name = envs.PRESETS[spec["preset"]]["os"] if body.key == "professor" and spec else envs.DEFAULT_ENV["os"]
    db.add(TaskEvent(attempt_id=attempt.id, kind="env_select", payload={"env": body.key, "os": os_name}))
    db.commit()
    if body.key == "professor" and spec is not None:
        envs.start_build(spec)
    return envs.env_info(db, e)


# ---------- 교수 ----------
@router.get("/api/assignments/{assignment_id}/environment")
def get_assignment_env(a: OwnedAssignment) -> dict[str, Any]:
    spec = envs.professor_spec(a.tool_config or {})
    return {
        "presets": {k: {kk: v[kk] for kk in ("os", "python", "cpp", "java")} for k, v in envs.PRESETS.items()},
        "spec": spec,
        "environment": envs.describe(spec) if spec else None,
        "dockerfile": envs.dockerfile(spec) if spec else "",
    }


@router.put("/api/assignments/{assignment_id}/environment")
def set_assignment_env(body: dict[str, Any] | None, a: OwnedAssignment, db: DB) -> dict[str, Any]:
    cfg = dict(a.tool_config or {})
    if not body:
        cfg.pop("environment", None)
        a.tool_config = cfg
        db.commit()
        return get_assignment_env(a)
    try:
        spec = envs.normalize(body)
    except envs.EnvError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    cfg["environment"] = spec
    a.tool_config = cfg  # JSON 열은 새 객체를 넣어야 바뀐 것으로 본다
    db.commit()
    envs.start_build(spec)
    return get_assignment_env(a)
