"""AI가 맡는 일. 라우터는 이 모듈의 함수만 부른다.

모든 함수는 AIUnavailable을 그대로 올린다. 라우터가 503으로 바꾼다.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..models import (
    AISummary,
    ArtifactVersion,
    Assignment,
    ChatMessage,
    Competency,
    CompetencyState,
    Enrollment,
    LearningCheck,
    Signal,
    TaskAttempt,
    TestItem,
)
from ..util import clamp_score, utcnow
from . import prompts
from .client import ai_client
from .retrieval import build_context
from .schemas import (
    CompetencyDraftList,
    GradeResult,
    ItemDraft,
    ItemDraftList,
    LeakJudgement,
    ProcessSummary,
    RefinedReply,
    SignalList,
    SocraticReply,
)


def _materials(assignment: Assignment) -> list[tuple[str, str]]:
    return [(m.filename, m.extracted_text) for m in assignment.materials]


def _context(assignment: Assignment, query: str) -> tuple[str, bool]:
    return build_context(_materials(assignment), query, max_chars=settings.ai_max_context_chars)


def _latest_artifact_text(attempt: TaskAttempt | None) -> str | None:
    if attempt is None:
        return None
    for v in reversed(attempt.versions):
        if v.text_content:
            return v.text_content
    return None


# ---------- 산파술 대화 ----------
def socratic_reply(
    db: Session,
    enrollment: Enrollment,
    stage: str,
    attempt: TaskAttempt | None,
    user_message: str,
) -> ChatMessage:
    assignment = enrollment.assignment
    attempt_no = attempt.attempt_no if attempt else 0
    history = [
        m
        for m in enrollment.messages
        if m.stage == stage and m.attempt_no == attempt_no
    ]
    # 라우터가 방금 저장한 학생 메시지는 아래에서 따로 붙이므로 뺀다
    if history and history[-1].role == "student" and history[-1].content == user_message:
        history = history[:-1]
    history = history[-20:]
    context, has_materials = _context(assignment, user_message)
    latest = _latest_artifact_text(attempt) if stage == "task" else None
    system = prompts.socratic_system(
        assignment, enrollment.competency_states, stage, context, has_materials, latest
    )
    messages: list[dict[str, Any]] = [
        {"role": "user" if m.role == "student" else "assistant", "content": m.content}
        for m in history
    ]
    messages.append({"role": "user", "content": user_message})
    messages = _merge_consecutive(messages)

    reply = ai_client.parse(
        purpose=f"chat_{stage}",
        system=system,
        messages=messages,
        output_model=SocraticReply,
        effort=settings.ai_effort_chat,
        db=db,
    )
    meta: dict[str, Any] = {
        "uncertain": reply.uncertain,
        "uses_materials": reply.uses_materials,
        "leaks_answer_self_check": reply.leaks_answer_self_check,
        "judged": False,
        "refined": False,
    }
    content = reply.content.strip()
    if stage == "task" and reply.mode != "refuse":
        judgement = ai_client.parse(
            purpose="judge_leak",
            system=prompts.judge_system(assignment, latest),
            messages=[{"role": "user", "content": f"학생 질문:\n{user_message}\n\nAI 응답:\n{content}"}],
            output_model=LeakJudgement,
            effort=settings.ai_effort_judge,
            max_tokens=800,
            db=db,
        )
        meta["judged"] = True
        meta["judge_reason"] = judgement.reason
        if judgement.leaks_answer:
            refined = ai_client.parse(
                purpose="refine_leak",
                system=prompts.refine_system(),
                messages=[{"role": "user", "content": f"학생 질문:\n{user_message}\n\n원래 응답:\n{content}"}],
                output_model=RefinedReply,
                effort=settings.ai_effort_chat,
                db=db,
            )
            meta["refined"] = True
            meta["original_content"] = content
            content = refined.content.strip()

    return ChatMessage(
        enrollment_id=enrollment.id,
        stage=stage,
        attempt_no=attempt_no,
        role="ai",
        content=content,
        mode=reply.mode,
        question_kind=reply.question_kind,
        meta=meta,
    )


def _merge_consecutive(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 역할이 연달아 오면 합친다. 첫 메시지는 user여야 한다."""
    merged: list[dict[str, Any]] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n\n" + m["content"]
        else:
            merged.append(dict(m))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


# ---------- 과제 설정 보조 ----------
def draft_competencies(db: Session, assignment: Assignment, hint: str) -> CompetencyDraftList:
    context, _ = _context(assignment, assignment.title + " " + assignment.description)
    body = (
        f"과목: {assignment.subject}\n제목: {assignment.title}\n종류: {assignment.task_kind}\n"
        f"설명:\n{assignment.description}\n\n교수의 힌트: {hint or '없음'}\n\n"
        f"학습 자료 발췌:\n{context or '없음'}"
    )
    return ai_client.parse(
        purpose="draft_competencies",
        system=prompts.competency_draft_system(),
        messages=[{"role": "user", "content": body}],
        output_model=CompetencyDraftList,
        effort=settings.ai_effort_grade,
        db=db,
    )


def draft_items(db: Session, assignment: Assignment, phase: str) -> list[ItemDraft]:
    context, _ = _context(assignment, assignment.title + " " + assignment.description)
    comps = "\n".join(
        f"- {c.name}: {c.description}\n  채점 기준: {c.grading_criteria}" for c in assignment.competencies
    )
    existing = [i for i in assignment.items if i.phase != phase]
    existing_text = "\n".join(f"- [{i.competency.name}] {i.stem}" for i in existing) or "없음"
    body = (
        f"과목: {assignment.subject}\n제목: {assignment.title}\n설명:\n{assignment.description}\n\n"
        f"역량과 채점 기준:\n{comps}\n\n다른 단계 문항, 겹치지 않게 낸다:\n{existing_text}\n\n"
        f"학습 자료 발췌:\n{context or '없음'}"
    )
    result = ai_client.parse(
        purpose=f"draft_items_{phase}",
        system=prompts.item_draft_system(phase),
        messages=[{"role": "user", "content": body}],
        output_model=ItemDraftList,
        effort=settings.ai_effort_grade,
        max_tokens=8000,
        db=db,
    )
    return result.items


# ---------- 채점 ----------
def grade_free_answer(
    db: Session,
    assignment: Assignment,
    competency: Competency,
    stem: str,
    model_answer: str,
    answer: str,
    item_type: str,
) -> GradeResult:
    context, has_materials = _context(assignment, stem)
    body = (
        f"역량: {competency.name}\n채점 기준:\n{competency.grading_criteria or '교수가 채점 기준을 쓰지 않았다. 모범 답안과 학습 자료를 기준으로 채점한다.'}\n\n"
        f"문항 종류: {item_type}\n문항:\n{stem}\n\n모범 답안 요지:\n{model_answer or '없음'}\n\n"
        f"학습 자료 발췌:\n{context if has_materials else '없음'}\n\n학생 답안:\n{answer.strip() or '(빈 답안)'}"
    )
    result = ai_client.parse(
        purpose="grade",
        system=prompts.grade_system(),
        messages=[{"role": "user", "content": body}],
        output_model=GradeResult,
        effort=settings.ai_effort_grade,
        max_tokens=1500,
        db=db,
    )
    result.score = int(clamp_score(result.score))
    return result


# ---------- 학습 단계 확인 문항 ----------
def make_check_question(db: Session, enrollment: Enrollment, competency: Competency) -> ItemDraft:
    assignment = enrollment.assignment
    context, has_materials = _context(assignment, competency.name + " " + competency.description)
    previous = [c for c in enrollment.checks if c.competency_id == competency.id]
    prev_text = "\n".join(f"- {c.item.get('stem', '')}" for c in previous) or "없음"
    pre_items = [i for i in assignment.items if i.competency_id == competency.id]
    pre_text = "\n".join(f"- {i.stem}" for i in pre_items) or "없음"
    recent = [m for m in enrollment.messages if m.stage == "learning"][-10:]
    chat_text = "\n".join(f"{'학생' if m.role == 'student' else 'AI'}: {m.content[:300]}" for m in recent) or "없음"
    body = (
        f"역량: {competency.name}\n설명: {competency.description}\n채점 기준:\n{competency.grading_criteria}\n\n"
        f"이전 확인 문항:\n{prev_text}\n\n사전 사후 평가 문항:\n{pre_text}\n\n최근 학습 대화:\n{chat_text}\n\n"
        f"학습 자료 발췌:\n{context if has_materials else '없음'}"
    )
    return ai_client.parse(
        purpose="check_question",
        system=prompts.check_question_system(),
        messages=[{"role": "user", "content": body}],
        output_model=ItemDraft,
        effort=settings.ai_effort_chat,
        max_tokens=1500,
        db=db,
    )


# ---------- 요약 ----------
def _states_text(states: list[CompetencyState]) -> str:
    lines = []
    for s in states:
        lines.append(
            f"- {s.competency.name}: 사전 {s.pre_score if s.pre_score is not None else '미측정'}, "
            f"현재 {s.current_score if s.current_score is not None else '미측정'}, "
            f"사후 {s.post_score if s.post_score is not None else '미측정'}, 통과 기준 {s.competency.pass_score}, "
            f"{'통과' if s.passed else '미통과'}"
        )
    return "\n".join(lines) or "없음"


def summarize_learning(db: Session, enrollment: Enrollment) -> AISummary:
    chats = [m for m in enrollment.messages if m.stage == "learning"]
    chat_text = "\n".join(
        f"{'학생' if m.role == 'student' else 'AI'}[{m.mode or '-'}]: {m.content[:400]}" for m in chats[-60:]
    ) or "대화 없음"
    checks_text = "\n".join(
        f"- [{c.competency.name}] 문항: {c.item.get('stem', '')[:200]} / 답: {(c.answer_text or '')[:200]} / 점수: {c.score}"
        for c in enrollment.checks
    ) or "확인 문항 없음"
    body = (
        f"역량 상태:\n{_states_text(enrollment.competency_states)}\n\n확인 문항 결과:\n{checks_text}\n\n"
        f"학습 대화:\n{chat_text}"
    )
    result = ai_client.parse(
        purpose="summary_learning",
        system=prompts.summary_system("learning"),
        messages=[{"role": "user", "content": body}],
        output_model=ProcessSummary,
        effort=settings.ai_effort_chat,
        max_tokens=3000,
        db=db,
    )
    return AISummary(enrollment_id=enrollment.id, kind="learning", attempt_no=0, content=result.model_dump())


def _versions_text(versions: list[ArtifactVersion], max_chars: int = 700) -> str:
    lines = []
    for v in versions:
        body = (v.text_content or "")[:max_chars]
        lines.append(
            f"### 버전 {v.version_no} ({v.source}, {v.kind}, {v.created_at:%Y-%m-%d %H:%M}) 변경 이유: {v.reason}\n"
            f"길이 {len(v.text_content or '')}자\n{body}"
        )
    return "\n\n".join(lines) or "버전 없음"


def summarize_task(db: Session, enrollment: Enrollment, attempt: TaskAttempt) -> AISummary:
    chats = [m for m in enrollment.messages if m.stage == "task" and m.attempt_no == attempt.attempt_no]
    chat_text = "\n".join(
        f"{'학생' if m.role == 'student' else 'AI'}[{m.mode or '-'}]: {m.content[:400]}" for m in chats[-60:]
    ) or "대화 없음"
    body = (
        f"시도 번호: {attempt.attempt_no}\n역량 상태:\n{_states_text(enrollment.competency_states)}\n\n"
        f"결과물 버전 이력:\n{_versions_text(attempt.versions)}\n\n과제 단계 대화:\n{chat_text}"
    )
    result = ai_client.parse(
        purpose="summary_task",
        system=prompts.summary_system("task"),
        messages=[{"role": "user", "content": body}],
        output_model=ProcessSummary,
        effort=settings.ai_effort_chat,
        max_tokens=3000,
        db=db,
    )
    return AISummary(
        enrollment_id=enrollment.id, kind="task", attempt_no=attempt.attempt_no, content=result.model_dump()
    )


# ---------- 위반 의심 신호 ----------
def detect_signals(db: Session, enrollment: Enrollment, attempt: TaskAttempt) -> list[Signal]:
    chats = [m for m in enrollment.messages if m.stage == "task" and m.attempt_no == attempt.attempt_no]
    chat_text = "\n".join(
        f"{'학생' if m.role == 'student' else 'AI'}[{m.mode or '-'}]: {m.content[:300]}" for m in chats[-60:]
    ) or "대화 없음"
    body = (
        f"시도 번호: {attempt.attempt_no}\n\n결과물 버전 이력:\n{_versions_text(attempt.versions, max_chars=500)}\n\n"
        f"과제 단계 대화:\n{chat_text}"
    )
    result = ai_client.parse(
        purpose="signals",
        system=prompts.signal_system(),
        messages=[{"role": "user", "content": body}],
        output_model=SignalList,
        effort=settings.ai_effort_judge,
        max_tokens=2000,
        db=db,
    )
    return [
        Signal(
            enrollment_id=enrollment.id,
            attempt_no=attempt.attempt_no,
            kind=s.kind,
            description=s.description,
            evidence={"text": s.evidence},
        )
        for s in result.signals
    ]


def item_to_check_json(item: ItemDraft) -> dict[str, Any]:
    return {
        "type": item.type,
        "stem": item.stem,
        "options": item.options,
        "answer": item.answer,
    }


def new_learning_check(enrollment: Enrollment, competency: Competency, item: ItemDraft) -> LearningCheck:
    return LearningCheck(
        enrollment_id=enrollment.id, competency_id=competency.id, item=item_to_check_json(item)
    )


def mark_answered(check: LearningCheck, answer: str, score: float, feedback: str) -> None:
    check.answer_text = answer
    check.score = score
    check.feedback = feedback
    check.answered_at = utcnow()


__all__ = [
    "TestItem",
    "socratic_reply",
    "draft_competencies",
    "draft_items",
    "grade_free_answer",
    "make_check_question",
    "summarize_learning",
    "summarize_task",
    "detect_signals",
    "new_learning_check",
    "mark_answered",
]
