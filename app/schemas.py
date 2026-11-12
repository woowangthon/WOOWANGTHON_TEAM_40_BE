"""요청과 응답 스키마."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Role = Literal["professor", "student"]
Phase = Literal["pre", "post"]
ItemType = Literal["choice", "short", "fill", "essay_short", "essay_long"]
RuleTarget = Literal["ai", "student", "both"]
TaskKind = Literal["performance", "essay"]


# ---------- 인증 ----------
class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=72)
    name: str = Field(min_length=1, max_length=100)
    role: Role

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("이메일 형식이 아니다")
        return v


class LoginIn(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: str

    model_config = {"from_attributes": True}


class TokenOut(BaseModel):
    token: str
    user: UserOut


# ---------- 과제 설정 ----------
class CompetencyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    pass_score: int = Field(default=60, ge=0, le=100)
    grading_criteria: str = ""


class CompetencyOut(CompetencyIn):
    id: int
    order: int

    model_config = {"from_attributes": True}


class CompetencyPublic(BaseModel):
    id: int
    name: str
    description: str
    pass_score: int

    model_config = {"from_attributes": True}


class ItemIn(BaseModel):
    competency_id: int
    type: ItemType
    stem: str = Field(min_length=1)
    options: list[str] = []
    answer: str = ""


class ItemOut(ItemIn):
    id: int
    phase: str
    order: int

    model_config = {"from_attributes": True}


class ItemPublic(BaseModel):
    id: int
    competency_id: int
    type: str
    stem: str
    options: list[str]

    model_config = {"from_attributes": True}


class MaterialOut(BaseModel):
    id: int
    filename: str
    mime: str
    size: int
    has_text: bool
    created_at: datetime


class RuleIn(BaseModel):
    target: RuleTarget
    text: str = Field(min_length=1)


class RuleOut(RuleIn):
    id: int

    model_config = {"from_attributes": True}


class AssignmentCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    subject: str = ""
    description: str = ""
    deadline: datetime | None = None
    task_kind: TaskKind = "performance"
    tool_slot: int = Field(default=0, ge=0, le=4)
    tool_name: str = "editor"
    tool_config: dict[str, Any] = {}
    pre_time_limit_min: int = Field(default=30, ge=1, le=600)
    post_time_limit_min: int = Field(default=30, ge=1, le=600)


class AssignmentUpdate(BaseModel):
    title: str | None = None
    subject: str | None = None
    description: str | None = None
    deadline: datetime | None = None
    clear_deadline: bool = False
    task_kind: TaskKind | None = None
    tool_slot: int | None = Field(default=None, ge=0, le=4)
    tool_name: str | None = None
    tool_config: dict[str, Any] | None = None
    pre_time_limit_min: int | None = Field(default=None, ge=1, le=600)
    post_time_limit_min: int | None = Field(default=None, ge=1, le=600)
    status: Literal["draft", "open", "closed"] | None = None


class AssignmentBrief(BaseModel):
    id: int
    title: str
    subject: str
    deadline: datetime | None
    join_code: str
    status: str
    task_kind: str
    tool_slot: int
    tool_name: str
    created_at: datetime
    enrollment_count: int = 0


class DashboardRow(AssignmentBrief):
    stage_counts: dict[str, int]
    submitted_count: int
    ungraded_count: int
    signal_count: int


class DashboardOut(BaseModel):
    assignment_count: int
    open_count: int
    student_count: int
    ungraded_count: int
    signal_count: int
    rows: list[DashboardRow]


class AssignmentOut(AssignmentBrief):
    description: str
    attachment_name: str
    tool_config: dict[str, Any]
    pre_time_limit_min: int
    post_time_limit_min: int
    competencies: list[CompetencyOut]
    items: list[ItemOut]
    materials: list[MaterialOut]
    rules: list[RuleOut]


class AssignmentStudentView(BaseModel):
    id: int
    title: str
    subject: str
    description: str
    attachment_name: str
    deadline: datetime | None
    status: str
    task_kind: str
    tool_slot: int
    tool_name: str
    tool_config: dict[str, Any]
    pre_time_limit_min: int
    post_time_limit_min: int
    competencies: list[CompetencyPublic]
    student_rules: list[str]
    material_names: list[str]
    professor_name: str


class CompetencyDraftIn(BaseModel):
    hint: str = ""


class CompetencyDraftOut(BaseModel):
    competencies: list[CompetencyIn]


class ItemDraftIn(BaseModel):
    phase: Phase


class ItemDraftOut(BaseModel):
    items: list[ItemIn]


# ---------- 참여와 상태 ----------
class JoinIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class CompetencyStateOut(BaseModel):
    competency_id: int
    name: str
    pass_score: int
    pre_score: float | None
    current_score: float | None
    post_score: float | None
    passed: bool


class VersionOut(BaseModel):
    id: int
    version_no: int
    source: str
    kind: str
    text_content: str | None
    file_name: str
    has_file: bool
    reason: str
    meta: dict[str, Any]
    created_at: datetime


class TaskAttemptOut(BaseModel):
    id: int
    attempt_no: int
    status: str
    started_at: datetime
    submitted_at: datetime | None
    versions: list[VersionOut]


class EnrollmentOut(BaseModel):
    id: int
    assignment_id: int
    stage: str
    joined_at: datetime
    deadline_passed: bool
    assignment: AssignmentStudentView
    states: list[CompetencyStateOut]
    attempts: list[TaskAttemptOut]


class EnrollmentBrief(BaseModel):
    id: int
    assignment_id: int
    stage: str
    joined_at: datetime
    deadline_passed: bool
    title: str
    subject: str
    professor_name: str
    deadline: datetime | None
    tool_slot: int
    tool_name: str


# ---------- 시험 ----------
class TestStartOut(BaseModel):
    attempt_id: int
    phase: str
    started_at: datetime
    time_limit_min: int
    items: list[ItemPublic]


class AnswerIn(BaseModel):
    item_id: int
    answer: str = ""


class TestSubmitIn(BaseModel):
    answers: list[AnswerIn]


class TestAnswerOut(BaseModel):
    id: int
    item_id: int
    competency_id: int
    type: str
    stem: str
    options: list[str]
    answer_text: str
    score: float | None
    feedback: str
    rationale: str
    graded_by: str
    expected_answer: str | None = None


class TestResultOut(BaseModel):
    attempt_id: int
    phase: str
    started_at: datetime
    submitted_at: datetime | None
    total_score: float | None
    competency_scores: dict[str, float]
    answers: list[TestAnswerOut]
    stage: str


# ---------- 대화와 학습 ----------
class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ChatMessageOut(BaseModel):
    id: int
    stage: str
    attempt_no: int
    role: str
    content: str
    mode: str
    question_kind: str
    meta: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class LearningCheckOut(BaseModel):
    id: int
    competency_id: int
    item: dict[str, Any]
    answer_text: str | None
    score: float | None
    feedback: str
    created_at: datetime
    answered_at: datetime | None


class LearningStateOut(BaseModel):
    enrollment_id: int
    stage: str
    states: list[CompetencyStateOut]
    messages: list[ChatMessageOut]
    checks: list[LearningCheckOut]
    can_finish: bool
    student_rules: list[str]


class CheckAnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=8000)


# ---------- 과제 단계 ----------
class VersionIn(BaseModel):
    text_content: str | None = None
    reason: str = Field(min_length=1, max_length=500)
    source: Literal["editor", "embed", "google", "companion"] = "editor"
    kind: Literal["text", "image", "file"] = "text"
    meta: dict[str, Any] = {}


class TaskStateOut(BaseModel):
    enrollment_id: int
    stage: str
    deadline: datetime | None
    deadline_passed: bool
    can_start_new_attempt: bool
    attempt: TaskAttemptOut | None
    attempts: list[TaskAttemptOut]
    messages: list[ChatMessageOut]
    tool_slot: int
    tool_name: str
    tool_config: dict[str, Any]
    task_kind: str
    description: str
    attachment_name: str
    student_rules: list[str]
    companion_connected: bool
    # 보조 프로그램이 서버에 마지막으로 접속한 시각과, 그 시각이 최근이어서 지금 돌고 있다고 볼 수 있는지
    companion_last_seen: datetime | None = None
    companion_live: bool = False
    google_connected: bool
    google_account: str = ""
    google_doc_id: str = ""
    google_doc_name: str = ""
    google_last_synced_at: datetime | None = None


# ---------- 보조 프로그램 ----------
class CompanionTokenOut(BaseModel):
    token: str


class CompanionMeOut(BaseModel):
    enrollment_id: int
    student_name: str
    assignment_title: str
    tool_config: dict[str, Any]
    attempt_no: int | None
    deadline: datetime | None
    deadline_passed: bool
    consent_text: str
    snapshot_seconds: int
    ai_panel_path: str


# ---------- 교수 화면 ----------
class SummaryOut(BaseModel):
    id: int
    kind: str
    attempt_no: int
    content: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class SignalOut(BaseModel):
    id: int
    attempt_no: int
    kind: str
    description: str
    evidence: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class GradeIn(BaseModel):
    score: float | None = Field(default=None, ge=0, le=100)
    comment: str = ""


class GradeOut(BaseModel):
    score: float | None
    comment: str
    updated_at: datetime

    model_config = {"from_attributes": True}


class AnswerOverrideIn(BaseModel):
    score: float = Field(ge=0, le=100)
    feedback: str = ""


class OverviewRow(BaseModel):
    enrollment_id: int
    student_name: str
    student_email: str
    stage: str
    pre_score: float | None
    post_score: float | None
    passed_count: int
    competency_count: int
    attempt_count: int
    version_count: int
    signal_count: int
    grade_score: float | None
    last_submitted_at: datetime | None


class OverviewOut(BaseModel):
    assignment: AssignmentBrief
    stage_counts: dict[str, int]
    rows: list[OverviewRow]


class StudentDetailOut(BaseModel):
    enrollment_id: int
    student: UserOut
    stage: str
    joined_at: datetime
    states: list[CompetencyStateOut]
    pre: TestResultOut | None
    post: TestResultOut | None
    checks: list[LearningCheckOut]
    summaries: list[SummaryOut]
    attempts: list[TaskAttemptOut]
    messages: list[ChatMessageOut]
    signals: list[SignalOut]
    grade: GradeOut | None
