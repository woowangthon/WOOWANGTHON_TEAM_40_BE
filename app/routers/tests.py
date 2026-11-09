"""사전평가와 사후평가. 응시는 1회, 제한 시간은 교수가 정하고, 시험 중에는 AI를 쓰지 않는다."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.orm import Session

from ..ai import services
from ..ai.client import AIUnavailable
from ..deps import DB, MyEnrollment
from ..models import Enrollment, TestAnswer, TestAttempt, TestItem
from ..schemas import ItemPublic, Phase, TestResultOut, TestStartOut, TestSubmitIn
from ..util import answer_matches, clamp_score, utcnow
from .common import ai_error, test_result_out

router = APIRouter(prefix="/api/enrollments/{enrollment_id}/tests", tags=["tests"])

GRACE_SECONDS = 60


def _required_stage(phase: str) -> str:
    return "pre" if phase == "pre" else "post"


def _open_attempt(e: Enrollment, phase: str) -> TestAttempt | None:
    for t in e.test_attempts:
        if t.phase == phase and t.submitted_at is None:
            return t
    return None


def _submitted_attempt(e: Enrollment, phase: str) -> TestAttempt | None:
    done = [t for t in e.test_attempts if t.phase == phase and t.submitted_at is not None]
    return done[-1] if done else None


@router.post("/{phase}/start", response_model=TestStartOut)
def start(phase: Phase, e: MyEnrollment, db: DB) -> TestStartOut:
    if _submitted_attempt(e, phase):
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 응시를 마친 시험이다")
    if e.stage != _required_stage(phase):
        raise HTTPException(status.HTTP_409_CONFLICT, "지금 단계에서는 이 시험을 볼 수 없다")
    a = e.assignment
    items = [i for i in a.items if i.phase == phase]
    if not items:
        raise HTTPException(status.HTTP_409_CONFLICT, "교수가 아직 문항을 만들지 않았다")
    attempt = _open_attempt(e, phase)
    if attempt is None:
        attempt = TestAttempt(enrollment_id=e.id, phase=phase)
        db.add(attempt)
        db.commit()
        db.refresh(attempt)
    limit = a.pre_time_limit_min if phase == "pre" else a.post_time_limit_min
    return TestStartOut(
        attempt_id=attempt.id,
        phase=phase,
        started_at=attempt.started_at,
        time_limit_min=limit,
        items=[ItemPublic.model_validate(i) for i in items],
    )


@router.post("/{phase}/submit", response_model=TestResultOut)
def submit(phase: Phase, body: TestSubmitIn, e: MyEnrollment, db: DB) -> TestResultOut:
    attempt = _open_attempt(e, phase)
    if attempt is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "시작한 시험이 없다")
    a = e.assignment
    limit = a.pre_time_limit_min if phase == "pre" else a.post_time_limit_min
    if utcnow() > attempt.started_at + timedelta(minutes=limit, seconds=GRACE_SECONDS):
        pass  # 제한 시간이 지나도 제출은 받되 시간 초과를 기록한다
    items = {i.id: i for i in a.items if i.phase == phase}
    given = {ans.item_id: ans.answer for ans in body.answers}
    try:
        grade_attempt(db, e, attempt, items, given)
    except AIUnavailable as exc:
        db.rollback()
        raise ai_error(exc) from exc
    attempt.submitted_at = utcnow()
    attempt.competency_scores = dict(attempt.competency_scores or {})
    if utcnow() > attempt.started_at + timedelta(minutes=limit, seconds=GRACE_SECONDS):
        attempt.competency_scores["_time_exceeded"] = 1
    _apply_scores(e, attempt, phase)
    db.commit()
    db.refresh(e)
    db.refresh(attempt)
    return test_result_out(attempt, e, reveal_answers=False)


@router.get("/{phase}", response_model=TestResultOut)
def result(phase: Phase, e: MyEnrollment) -> TestResultOut:
    attempt = _submitted_attempt(e, phase)
    if attempt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "응시 기록이 없다")
    return test_result_out(attempt, e, reveal_answers=False)


def grade_attempt(
    db: Session,
    e: Enrollment,
    attempt: TestAttempt,
    items: dict[int, TestItem],
    given: dict[int, str],
) -> None:
    """문항별 채점. 선택형은 자동, 단답형과 완성형은 정답 비교, 서술형과 논술형은 AI가 채점 기준으로 채점한다."""
    for old in list(attempt.answers):
        db.delete(old)
    db.flush()
    per_comp: dict[int, list[float]] = {}
    for item_id, item in items.items():
        text = (given.get(item_id) or "").strip()
        ans = TestAnswer(attempt_id=attempt.id, item_id=item.id, answer_text=text)
        if item.type == "choice":
            ans.score = 100.0 if answer_matches(item.answer, text) else 0.0
            ans.graded_by = "auto"
        elif item.type in ("short", "fill"):
            ans.score = 100.0 if answer_matches(item.answer, text) else 0.0
            ans.graded_by = "auto"
        else:
            if not text:
                ans.score, ans.graded_by, ans.feedback = 0.0, "auto", "답안이 비어 있다"
            else:
                result = services.grade_free_answer(
                    db, e.assignment, item.competency, item.stem, item.answer, text, item.type
                )
                ans.score = clamp_score(result.score)
                ans.rationale = result.rationale
                ans.feedback = result.feedback
                ans.graded_by = "ai"
        db.add(ans)
        per_comp.setdefault(item.competency_id, []).append(ans.score or 0.0)
    scores = {str(cid): round(sum(v) / len(v), 1) for cid, v in per_comp.items() if v}
    attempt.competency_scores = scores
    attempt.total_score = round(sum(scores.values()) / len(scores), 1) if scores else 0.0


def _apply_scores(e: Enrollment, attempt: TestAttempt, phase: str) -> None:
    scores = {k: v for k, v in (attempt.competency_scores or {}).items() if not k.startswith("_")}
    for s in e.competency_states:
        value = scores.get(str(s.competency_id))
        if value is None:
            continue
        if phase == "pre":
            s.pre_score = float(value)
            s.current_score = float(value)
            s.passed = float(value) >= s.competency.pass_score
        else:
            s.post_score = float(value)
    if phase == "pre":
        e.stage = "task" if all(s.passed for s in e.competency_states) else "learning"
    else:
        e.stage = "done"
