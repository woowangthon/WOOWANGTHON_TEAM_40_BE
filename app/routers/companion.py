"""자리 3 보조 프로그램이 부르는 API. 학생이 웹에서 발급한 토큰으로 인증한다."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from ..config import settings
from ..deps import DB, CompanionEnrollment, MyEnrollment
from ..models import CompanionToken
from ..schemas import ChatIn, ChatMessageOut, CompanionMeOut, CompanionTokenOut, TaskStateOut
from ..util import new_token
from .common import deadline_passed, message_out
from .tasks import add_file_version, current_attempt, require_open_attempt, run_chat, task_state

router = APIRouter(tags=["companion"])

CONSENT_TEXT = (
    "이 프로그램은 과제 기록을 위해 다음을 서버로 보낸다. 선택한 프로그램 창의 캡처 이미지, "
    "선택한 저장 폴더에 저장되는 결과물 파일, 그리고 AI 창에서 주고받은 대화. 다른 창이나 폴더는 기록하지 않는다. "
    "기록은 과제의 교수가 평가에 쓰며 기록 시작 뒤 언제든 프로그램을 종료해 중단할 수 있다."
)


@router.post("/api/enrollments/{enrollment_id}/companion/token", response_model=CompanionTokenOut)
def issue_token(e: MyEnrollment, db: DB) -> CompanionTokenOut:
    if e.assignment.tool_slot != 3:
        raise HTTPException(status.HTTP_409_CONFLICT, "이 과제는 보조 프로그램을 쓰지 않는다")
    token = CompanionToken(enrollment_id=e.id, token=new_token())
    db.add(token)
    db.commit()
    return CompanionTokenOut(token=token.token)


@router.get("/api/companion/me", response_model=CompanionMeOut)
def me(e: CompanionEnrollment) -> CompanionMeOut:
    a = e.assignment
    attempt = current_attempt(e)
    return CompanionMeOut(
        enrollment_id=e.id,
        student_name=e.student.name,
        assignment_title=a.title,
        tool_config=a.tool_config or {},
        attempt_no=attempt.attempt_no if attempt else None,
        deadline=a.deadline,
        deadline_passed=deadline_passed(a),
        consent_text=CONSENT_TEXT,
        snapshot_seconds=settings.companion_snapshot_seconds,
        ai_panel_path=f"/companion-panel/{e.id}",
    )


@router.get("/api/companion/task", response_model=TaskStateOut)
def task(e: CompanionEnrollment, db: DB) -> TaskStateOut:
    return task_state(db, e)


@router.post("/api/companion/versions", response_model=TaskStateOut, status_code=status.HTTP_201_CREATED)
async def upload_version(
    e: CompanionEnrollment,
    db: DB,
    file: Annotated[UploadFile, File()],
    reason: Annotated[str, Form()] = "저장 폴더에서 감지한 저장",
) -> TaskStateOut:
    attempt = require_open_attempt(e)
    await add_file_version(db, e, attempt, file, reason, "companion", {"from": "folder_watch"})
    db.refresh(e)
    return task_state(db, e)


@router.post("/api/companion/snapshots", response_model=TaskStateOut, status_code=status.HTTP_201_CREATED)
async def upload_snapshot(
    e: CompanionEnrollment,
    db: DB,
    file: Annotated[UploadFile, File()],
    window_title: Annotated[str, Form()] = "",
) -> TaskStateOut:
    attempt = require_open_attempt(e)
    await add_file_version(
        db, e, attempt, file, "자동 창 캡처", "companion", {"snapshot": True, "window_title": window_title}
    )
    db.refresh(e)
    return task_state(db, e)


@router.get("/api/companion/messages", response_model=list[ChatMessageOut])
def messages(e: CompanionEnrollment) -> list[ChatMessageOut]:
    attempt = current_attempt(e)
    no = attempt.attempt_no if attempt else 0
    return [message_out(m) for m in e.messages if m.stage == "task" and m.attempt_no == no]


@router.post("/api/companion/chat", response_model=list[ChatMessageOut])
def chat(body: ChatIn, e: CompanionEnrollment, db: DB) -> list[ChatMessageOut]:
    return run_chat(db, e, body.message)
