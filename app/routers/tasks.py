"""과제 단계. 시도마다 결과물 버전과 AI 대화를 나누어 기록한다."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..ai import services
from ..ai.client import AIUnavailable
from ..ai.extract import extract_text, is_image
from ..config import settings
from ..deps import DB, MyEnrollment
from ..models import ArtifactVersion, ChatMessage, CompanionToken, Enrollment, GoogleConnection, TaskAttempt
from ..schemas import ChatIn, ChatMessageOut, TaskStateOut, VersionIn
from ..storage import save_upload
from ..util import utcnow
from .common import ai_error, attempt_out, deadline_passed, message_out, student_rules

router = APIRouter(prefix="/api/enrollments/{enrollment_id}/task", tags=["task"])

TASK_STAGES = {"task", "post", "done"}


class SubmitOut(BaseModel):
    task: TaskStateOut
    warning: str | None = None


def current_attempt(e: Enrollment) -> TaskAttempt | None:
    for t in e.task_attempts:
        if t.status == "in_progress":
            return t
    return None


def ensure_first_attempt(db: Session, e: Enrollment) -> None:
    if e.stage == "task" and not e.task_attempts:
        db.add(TaskAttempt(enrollment_id=e.id, attempt_no=1))
        db.commit()
        db.refresh(e)


def can_start_new_attempt(e: Enrollment) -> bool:
    return e.stage == "done" and current_attempt(e) is None and not deadline_passed(e.assignment)


def task_state(db: Session, e: Enrollment) -> TaskStateOut:
    a = e.assignment
    attempt = current_attempt(e)
    attempt_no = attempt.attempt_no if attempt else (e.task_attempts[-1].attempt_no if e.task_attempts else 0)
    msgs = [m for m in e.messages if m.stage == "task" and m.attempt_no == attempt_no]
    companion = db.query(CompanionToken).filter(CompanionToken.enrollment_id == e.id).count() > 0
    google = db.query(GoogleConnection).filter(GoogleConnection.enrollment_id == e.id).count() > 0
    return TaskStateOut(
        enrollment_id=e.id,
        stage=e.stage,
        deadline=a.deadline,
        deadline_passed=deadline_passed(a),
        can_start_new_attempt=can_start_new_attempt(e),
        attempt=attempt_out(attempt) if attempt else None,
        attempts=[attempt_out(t) for t in e.task_attempts],
        messages=[message_out(m) for m in msgs],
        tool_slot=a.tool_slot,
        tool_name=a.tool_name,
        tool_config=a.tool_config or {},
        task_kind=a.task_kind,
        description=a.description,
        attachment_name=a.attachment_name,
        student_rules=student_rules(a),
        companion_connected=companion,
        google_connected=google,
    )


def require_task_stage(e: Enrollment) -> None:
    if e.stage not in TASK_STAGES:
        raise HTTPException(status.HTTP_409_CONFLICT, "학습 단계를 마쳐야 과제 단계로 갈 수 있다")


def require_open_attempt(e: Enrollment) -> TaskAttempt:
    require_task_stage(e)
    if deadline_passed(e.assignment):
        raise HTTPException(status.HTTP_409_CONFLICT, "마감이 지나 수정할 수 없다")
    attempt = current_attempt(e)
    if attempt is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "진행 중인 시도가 없다. 새 시도를 시작해야 한다")
    return attempt


@router.get("", response_model=TaskStateOut)
def get_task(e: MyEnrollment, db: DB) -> TaskStateOut:
    require_task_stage(e)
    ensure_first_attempt(db, e)
    return task_state(db, e)


@router.post("/attempts", response_model=TaskStateOut, status_code=status.HTTP_201_CREATED)
def new_attempt(e: MyEnrollment, db: DB) -> TaskStateOut:
    require_task_stage(e)
    if deadline_passed(e.assignment):
        raise HTTPException(status.HTTP_409_CONFLICT, "마감이 지나 재제출할 수 없다")
    if current_attempt(e) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "진행 중인 시도가 이미 있다")
    if e.stage != "done":
        raise HTTPException(status.HTTP_409_CONFLICT, "첫 제출 뒤 사후평가를 마쳐야 재제출할 수 있다")
    next_no = (e.task_attempts[-1].attempt_no + 1) if e.task_attempts else 1
    db.add(TaskAttempt(enrollment_id=e.id, attempt_no=next_no))
    db.commit()
    db.refresh(e)
    return task_state(db, e)


def add_text_version(
    db: Session, attempt: TaskAttempt, text: str | None, reason: str, source: str, kind: str, meta: dict[str, Any]
) -> ArtifactVersion:
    v = ArtifactVersion(
        attempt_id=attempt.id,
        version_no=len(attempt.versions) + 1,
        source=source,
        kind=kind,
        text_content=text,
        reason=reason.strip(),
        meta=meta,
    )
    db.add(v)
    db.commit()
    db.refresh(attempt)
    return v


async def add_file_version(
    db: Session, e: Enrollment, attempt: TaskAttempt, upload: UploadFile, reason: str, source: str, meta: dict[str, Any]
) -> ArtifactVersion:
    path, original, size = await save_upload(
        upload, f"enrollments/{e.id}/attempt{attempt.attempt_no}", settings.max_version_mb
    )
    kind = "image" if is_image(path) else "file"
    text = None if kind == "image" else (extract_text(path) or None)
    v = ArtifactVersion(
        attempt_id=attempt.id,
        version_no=len(attempt.versions) + 1,
        source=source,
        kind=kind,
        text_content=text,
        file_name=original,
        file_path=str(path),
        reason=reason.strip(),
        meta={**meta, "size": size},
    )
    db.add(v)
    db.commit()
    db.refresh(attempt)
    return v


@router.post("/versions", response_model=TaskStateOut, status_code=status.HTTP_201_CREATED)
def save_version(body: VersionIn, e: MyEnrollment, db: DB) -> TaskStateOut:
    attempt = require_open_attempt(e)
    if body.kind == "text" and not (body.text_content or "").strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "내용이 비어 있다")
    add_text_version(db, attempt, body.text_content, body.reason, body.source, body.kind, body.meta)
    db.refresh(e)
    return task_state(db, e)


@router.post("/versions/file", response_model=TaskStateOut, status_code=status.HTTP_201_CREATED)
async def save_file_version(
    e: MyEnrollment,
    db: DB,
    file: Annotated[UploadFile, File()],
    reason: Annotated[str, Form()],
    source: Annotated[str, Form()] = "embed",
) -> TaskStateOut:
    attempt = require_open_attempt(e)
    if not reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "변경 이유가 필요하다")
    await add_file_version(db, e, attempt, file, reason, source, {})
    db.refresh(e)
    return task_state(db, e)


@router.get("/versions/{version_id}/file")
def version_file(version_id: int, e: MyEnrollment, db: DB) -> FileResponse:
    v = db.get(ArtifactVersion, version_id)
    if v is None or v.attempt.enrollment_id != e.id or not v.file_path or not Path(v.file_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없다")
    return FileResponse(v.file_path, filename=v.file_name)


def run_chat(db: Session, e: Enrollment, message: str) -> list[ChatMessageOut]:
    attempt = require_open_attempt(e)
    student_msg = ChatMessage(
        enrollment_id=e.id, stage="task", attempt_no=attempt.attempt_no, role="student", content=message.strip()
    )
    db.add(student_msg)
    db.flush()
    try:
        ai_msg = services.socratic_reply(db, e, "task", attempt, message.strip())
    except AIUnavailable as exc:
        db.rollback()
        raise ai_error(exc) from exc
    db.add(ai_msg)
    db.commit()
    db.refresh(student_msg)
    db.refresh(ai_msg)
    return [message_out(student_msg), message_out(ai_msg)]


@router.post("/chat", response_model=list[ChatMessageOut])
def chat(body: ChatIn, e: MyEnrollment, db: DB) -> list[ChatMessageOut]:
    return run_chat(db, e, body.message)


def submit_attempt(db: Session, e: Enrollment) -> SubmitOut:
    attempt = require_open_attempt(e)
    if not attempt.versions:
        raise HTTPException(status.HTTP_409_CONFLICT, "저장된 버전이 하나도 없다")
    attempt.status = "submitted"
    attempt.submitted_at = utcnow()
    if e.stage == "task":
        e.stage = "post"
    db.commit()
    warning: str | None = None
    try:
        db.add(services.summarize_task(db, e, attempt))
        for s in services.detect_signals(db, e, attempt):
            db.add(s)
        db.commit()
    except AIUnavailable as exc:
        db.rollback()
        warning = f"제출은 되었지만 AI 요약과 의심 신호 생성에 실패했다: {exc}"
    db.refresh(e)
    return SubmitOut(task=task_state(db, e), warning=warning)


@router.post("/submit", response_model=SubmitOut)
def submit(e: MyEnrollment, db: DB) -> SubmitOut:
    return submit_attempt(db, e)
