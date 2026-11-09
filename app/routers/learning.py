"""학습 단계. 부족한 역량을 산파술 대화로 채우고 확인 문항으로 점수를 갱신한다."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..ai import services
from ..ai.client import AIUnavailable
from ..deps import DB, MyEnrollment
from ..models import ChatMessage, Competency, LearningCheck
from ..schemas import (
    ChatIn,
    ChatMessageOut,
    CheckAnswerIn,
    LearningCheckOut,
    LearningStateOut,
)
from ..util import answer_matches, clamp_score
from .common import ai_error, check_out, message_out, state_out, student_rules

router = APIRouter(prefix="/api/enrollments/{enrollment_id}/learning", tags=["learning"])

STAGES_AFTER_PRE = {"learning", "task", "post", "done"}


def _state(e) -> LearningStateOut:  # type: ignore[no-untyped-def]
    states = sorted(e.competency_states, key=lambda s: s.competency.order)
    return LearningStateOut(
        enrollment_id=e.id,
        stage=e.stage,
        states=[state_out(s) for s in states],
        messages=[message_out(m) for m in e.messages if m.stage == "learning"],
        checks=[check_out(c, reveal_answer=False) for c in e.checks],
        can_finish=e.stage == "learning" and all(s.passed for s in states),
        student_rules=student_rules(e.assignment),
    )


@router.get("", response_model=LearningStateOut)
def get_state(e: MyEnrollment) -> LearningStateOut:
    if e.stage not in STAGES_AFTER_PRE:
        raise HTTPException(status.HTTP_409_CONFLICT, "사전평가를 먼저 마쳐야 한다")
    return _state(e)


@router.post("/chat", response_model=list[ChatMessageOut])
def chat(body: ChatIn, e: MyEnrollment, db: DB) -> list[ChatMessageOut]:
    if e.stage != "learning":
        raise HTTPException(status.HTTP_409_CONFLICT, "학습 단계에서만 대화할 수 있다")
    student_msg = ChatMessage(
        enrollment_id=e.id, stage="learning", attempt_no=0, role="student", content=body.message.strip()
    )
    db.add(student_msg)
    db.flush()
    try:
        ai_msg = services.socratic_reply(db, e, "learning", None, body.message.strip())
    except AIUnavailable as exc:
        db.rollback()
        raise ai_error(exc) from exc
    db.add(ai_msg)
    db.commit()
    db.refresh(student_msg)
    db.refresh(ai_msg)
    return [message_out(student_msg), message_out(ai_msg)]


@router.post("/check/{competency_id}", response_model=LearningCheckOut, status_code=status.HTTP_201_CREATED)
def new_check(competency_id: int, e: MyEnrollment, db: DB) -> LearningCheckOut:
    if e.stage != "learning":
        raise HTTPException(status.HTTP_409_CONFLICT, "학습 단계에서만 확인 문항을 받을 수 있다")
    comp = db.get(Competency, competency_id)
    if comp is None or comp.assignment_id != e.assignment_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "역량을 찾을 수 없다")
    pending = [c for c in e.checks if c.competency_id == comp.id and c.answered_at is None]
    if pending:
        return check_out(pending[-1], reveal_answer=False)
    try:
        item = services.make_check_question(db, e, comp)
    except AIUnavailable as exc:
        raise ai_error(exc) from exc
    check = services.new_learning_check(e, comp, item)
    db.add(check)
    db.commit()
    db.refresh(check)
    return check_out(check, reveal_answer=False)


@router.post("/check/{check_id}/answer", response_model=LearningStateOut)
def answer_check(check_id: int, body: CheckAnswerIn, e: MyEnrollment, db: DB) -> LearningStateOut:
    check = db.get(LearningCheck, check_id)
    if check is None or check.enrollment_id != e.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "확인 문항을 찾을 수 없다")
    if check.answered_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 답한 문항이다")
    item = check.item or {}
    answer = body.answer.strip()
    comp = check.competency
    if item.get("type") in ("short", "fill", "choice") and answer_matches(item.get("answer", ""), answer):
        score, feedback = 100.0, "정답이다"
    else:
        try:
            result = services.grade_free_answer(
                db, e.assignment, comp, item.get("stem", ""), item.get("answer", ""), answer, item.get("type", "essay_short")
            )
        except AIUnavailable as exc:
            raise ai_error(exc) from exc
        score, feedback = clamp_score(result.score), result.feedback
    services.mark_answered(check, answer, score, feedback)
    for s in e.competency_states:
        if s.competency_id == comp.id:
            s.current_score = score
            s.passed = score >= comp.pass_score
    db.commit()
    db.refresh(e)
    return _state(e)


@router.post("/finish", response_model=LearningStateOut)
def finish(e: MyEnrollment, db: DB) -> LearningStateOut:
    if e.stage != "learning":
        raise HTTPException(status.HTTP_409_CONFLICT, "학습 단계가 아니다")
    if not all(s.passed for s in e.competency_states):
        raise HTTPException(status.HTTP_409_CONFLICT, "모든 역량이 통과 점수를 넘어야 과제 단계로 갈 수 있다")
    try:
        summary = services.summarize_learning(db, e)
    except AIUnavailable as exc:
        raise ai_error(exc) from exc
    db.add(summary)
    e.stage = "task"
    db.commit()
    db.refresh(e)
    return _state(e)
