"""라우터가 함께 쓰는 변환 함수."""

from __future__ import annotations

from fastapi import HTTPException, status

from ..ai.client import AIUnavailable
from ..models import (
    AIRule,
    ArtifactVersion,
    Assignment,
    ChatMessage,
    CompetencyState,
    Enrollment,
    LearningCheck,
    TaskAttempt,
    TestAttempt,
)
from ..schemas import (
    AssignmentBrief,
    AssignmentOut,
    AssignmentStudentView,
    ChatMessageOut,
    CompetencyOut,
    CompetencyPublic,
    CompetencyStateOut,
    ItemOut,
    LearningCheckOut,
    MaterialOut,
    RuleOut,
    TaskAttemptOut,
    TestAnswerOut,
    TestResultOut,
    VersionOut,
)
from ..util import utcnow


def ai_error(exc: AIUnavailable) -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def deadline_passed(assignment: Assignment) -> bool:
    return assignment.deadline is not None and utcnow() > assignment.deadline


def student_rules(assignment: Assignment) -> list[str]:
    return [r.text for r in assignment.rules if r.target in ("student", "both")]


def assignment_brief(a: Assignment) -> AssignmentBrief:
    return AssignmentBrief(
        id=a.id,
        title=a.title,
        subject=a.subject,
        deadline=a.deadline,
        join_code=a.join_code,
        status=a.status,
        task_kind=a.task_kind,
        tool_slot=a.tool_slot,
        tool_name=a.tool_name,
        created_at=a.created_at,
        enrollment_count=len(a.enrollments),
    )


def assignment_out(a: Assignment) -> AssignmentOut:
    return AssignmentOut(
        **assignment_brief(a).model_dump(),
        description=a.description,
        attachment_name=a.attachment_name,
        tool_config=a.tool_config or {},
        pre_time_limit_min=a.pre_time_limit_min,
        post_time_limit_min=a.post_time_limit_min,
        competencies=[CompetencyOut.model_validate(c) for c in a.competencies],
        items=[ItemOut.model_validate(i) for i in a.items],
        materials=[
            MaterialOut(
                id=m.id,
                filename=m.filename,
                mime=m.mime,
                size=m.size,
                has_text=bool(m.extracted_text),
                created_at=m.created_at,
            )
            for m in a.materials
        ],
        rules=[RuleOut.model_validate(r) for r in a.rules],
    )


def assignment_student_view(a: Assignment, professor_name: str) -> AssignmentStudentView:
    return AssignmentStudentView(
        id=a.id,
        title=a.title,
        subject=a.subject,
        description=a.description,
        attachment_name=a.attachment_name,
        deadline=a.deadline,
        status=a.status,
        task_kind=a.task_kind,
        tool_slot=a.tool_slot,
        tool_name=a.tool_name,
        tool_config=a.tool_config or {},
        pre_time_limit_min=a.pre_time_limit_min,
        post_time_limit_min=a.post_time_limit_min,
        competencies=[CompetencyPublic.model_validate(c) for c in a.competencies],
        student_rules=student_rules(a),
        material_names=[m.filename for m in a.materials],
        professor_name=professor_name,
    )


def state_out(s: CompetencyState) -> CompetencyStateOut:
    return CompetencyStateOut(
        competency_id=s.competency_id,
        name=s.competency.name,
        pass_score=s.competency.pass_score,
        pre_score=s.pre_score,
        current_score=s.current_score,
        post_score=s.post_score,
        passed=s.passed,
    )


def version_out(v: ArtifactVersion) -> VersionOut:
    return VersionOut(
        id=v.id,
        version_no=v.version_no,
        source=v.source,
        kind=v.kind,
        text_content=v.text_content,
        file_name=v.file_name,
        has_file=bool(v.file_path),
        reason=v.reason,
        meta=v.meta or {},
        created_at=v.created_at,
    )


def attempt_out(t: TaskAttempt) -> TaskAttemptOut:
    return TaskAttemptOut(
        id=t.id,
        attempt_no=t.attempt_no,
        status=t.status,
        started_at=t.started_at,
        submitted_at=t.submitted_at,
        versions=[version_out(v) for v in t.versions],
    )


def message_out(m: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut.model_validate(m)


def check_out(c: LearningCheck, reveal_answer: bool) -> LearningCheckOut:
    item = dict(c.item or {})
    if not reveal_answer and c.answered_at is None:
        item.pop("answer", None)
    return LearningCheckOut(
        id=c.id,
        competency_id=c.competency_id,
        item=item,
        answer_text=c.answer_text,
        score=c.score,
        feedback=c.feedback,
        created_at=c.created_at,
        answered_at=c.answered_at,
    )


def test_result_out(attempt: TestAttempt, enrollment: Enrollment, reveal_answers: bool) -> TestResultOut:
    answers = []
    for a in attempt.answers:
        answers.append(
            TestAnswerOut(
                id=a.id,
                item_id=a.item_id,
                competency_id=a.item.competency_id,
                type=a.item.type,
                stem=a.item.stem,
                options=a.item.options or [],
                answer_text=a.answer_text,
                score=a.score,
                feedback=a.feedback,
                rationale=a.rationale if reveal_answers else "",
                graded_by=a.graded_by,
                expected_answer=a.item.answer if reveal_answers else None,
            )
        )
    return TestResultOut(
        attempt_id=attempt.id,
        phase=attempt.phase,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        total_score=attempt.total_score,
        competency_scores={k: float(v) for k, v in (attempt.competency_scores or {}).items()},
        answers=answers,
        stage=enrollment.stage,
    )


def rules_of(assignment: Assignment, target: str) -> list[AIRule]:
    return [r for r in assignment.rules if r.target == target]
