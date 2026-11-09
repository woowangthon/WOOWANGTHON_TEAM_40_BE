"""교수 화면. 반 전체 지표와 개별 학생 지표, 과제 점수와 코멘트 입력."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from ..deps import DB, OwnedAssignment
from ..models import ArtifactVersion, Enrollment, ProfessorGrade, TestAnswer
from ..schemas import (
    AnswerOverrideIn,
    GradeIn,
    GradeOut,
    OverviewOut,
    OverviewRow,
    SignalOut,
    StudentDetailOut,
    SummaryOut,
    UserOut,
)
from .common import (
    assignment_brief,
    attempt_out,
    check_out,
    message_out,
    state_out,
    test_result_out,
)

router = APIRouter(prefix="/api/assignments/{assignment_id}", tags=["professor"])


def _latest(e: Enrollment, phase: str):  # type: ignore[no-untyped-def]
    done = [t for t in e.test_attempts if t.phase == phase and t.submitted_at is not None]
    return done[-1] if done else None


@router.get("/overview", response_model=OverviewOut)
def overview(a: OwnedAssignment) -> OverviewOut:
    counts = {"pre": 0, "learning": 0, "task": 0, "post": 0, "done": 0}
    rows: list[OverviewRow] = []
    for e in a.enrollments:
        counts[e.stage] = counts.get(e.stage, 0) + 1
        pre, post = _latest(e, "pre"), _latest(e, "post")
        submitted = [t.submitted_at for t in e.task_attempts if t.submitted_at]
        rows.append(
            OverviewRow(
                enrollment_id=e.id,
                student_name=e.student.name,
                student_email=e.student.email,
                stage=e.stage,
                pre_score=pre.total_score if pre else None,
                post_score=post.total_score if post else None,
                passed_count=sum(1 for s in e.competency_states if s.passed),
                competency_count=len(e.competency_states),
                attempt_count=len(e.task_attempts),
                version_count=sum(len(t.versions) for t in e.task_attempts),
                signal_count=len(e.signals),
                grade_score=e.grade.score if e.grade else None,
                last_submitted_at=max(submitted) if submitted else None,
            )
        )
    rows.sort(key=lambda r: r.student_name)
    return OverviewOut(assignment=assignment_brief(a), stage_counts=counts, rows=rows)


def _enrollment(a, enrollment_id: int, db) -> Enrollment:  # type: ignore[no-untyped-def]
    e = db.get(Enrollment, enrollment_id)
    if e is None or e.assignment_id != a.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "학생을 찾을 수 없다")
    return e


@router.get("/students/{enrollment_id}", response_model=StudentDetailOut)
def student_detail(enrollment_id: int, a: OwnedAssignment, db: DB) -> StudentDetailOut:
    e = _enrollment(a, enrollment_id, db)
    pre, post = _latest(e, "pre"), _latest(e, "post")
    return StudentDetailOut(
        enrollment_id=e.id,
        student=UserOut.model_validate(e.student),
        stage=e.stage,
        joined_at=e.joined_at,
        states=[state_out(s) for s in sorted(e.competency_states, key=lambda s: s.competency.order)],
        pre=test_result_out(pre, e, reveal_answers=True) if pre else None,
        post=test_result_out(post, e, reveal_answers=True) if post else None,
        checks=[check_out(c, reveal_answer=True) for c in e.checks],
        summaries=[SummaryOut.model_validate(s) for s in e.summaries],
        attempts=[attempt_out(t) for t in e.task_attempts],
        messages=[message_out(m) for m in e.messages],
        signals=[SignalOut.model_validate(s) for s in e.signals],
        grade=GradeOut.model_validate(e.grade) if e.grade else None,
    )


@router.put("/students/{enrollment_id}/grade", response_model=GradeOut)
def set_grade(enrollment_id: int, body: GradeIn, a: OwnedAssignment, db: DB) -> GradeOut:
    e = _enrollment(a, enrollment_id, db)
    if e.grade is None:
        e.grade = ProfessorGrade(enrollment_id=e.id)
        db.add(e.grade)
    e.grade.score = body.score
    e.grade.comment = body.comment
    db.commit()
    db.refresh(e.grade)
    return GradeOut.model_validate(e.grade)


@router.patch("/answers/{answer_id}", response_model=StudentDetailOut)
def override_answer(answer_id: int, body: AnswerOverrideIn, a: OwnedAssignment, db: DB) -> StudentDetailOut:
    """서술형과 논술형 AI 채점을 교수가 확인하고 고친다. 역량 점수와 상태도 다시 계산한다."""
    ans = db.get(TestAnswer, answer_id)
    if ans is None or ans.attempt.enrollment.assignment_id != a.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "답안을 찾을 수 없다")
    ans.score = float(body.score)
    if body.feedback:
        ans.feedback = body.feedback
    ans.graded_by = "professor"
    attempt = ans.attempt
    per_comp: dict[int, list[float]] = {}
    for x in attempt.answers:
        per_comp.setdefault(x.item.competency_id, []).append(x.score or 0.0)
    scores = {str(cid): round(sum(v) / len(v), 1) for cid, v in per_comp.items() if v}
    extra = {k: v for k, v in (attempt.competency_scores or {}).items() if k.startswith("_")}
    attempt.competency_scores = {**scores, **extra}
    attempt.total_score = round(sum(scores.values()) / len(scores), 1) if scores else 0.0
    e = attempt.enrollment
    for s in e.competency_states:
        value = scores.get(str(s.competency_id))
        if value is None:
            continue
        if attempt.phase == "pre":
            remeasured = s.current_score is not None and s.pre_score is not None and s.current_score != s.pre_score
            s.pre_score = value
            if not remeasured:
                s.current_score = value
                s.passed = value >= s.competency.pass_score
        else:
            s.post_score = value
    if attempt.phase == "pre" and e.stage == "learning" and all(s.passed for s in e.competency_states):
        e.stage = "task"
    db.commit()
    return student_detail(e.id, a, db)


@router.get("/students/{enrollment_id}/versions/{version_id}/file")
def version_file(enrollment_id: int, version_id: int, a: OwnedAssignment, db: DB) -> FileResponse:
    e = _enrollment(a, enrollment_id, db)
    v = db.get(ArtifactVersion, version_id)
    if v is None or v.attempt.enrollment_id != e.id or not v.file_path or not Path(v.file_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없다")
    return FileResponse(v.file_path, filename=v.file_name)
