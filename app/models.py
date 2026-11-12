"""DB 모델. 기획 5장 확정 사항의 용어를 그대로 쓴다.

단계 stage: pre(사전평가) → learning(학습) → task(과제) → post(사후평가) → done(첫 제출 완료, 재제출 가능)
도구 자리 tool_slot: 0 페이지 안 문서 편집기, 1 페이지 안 웹 도구, 2 API 연동 웹 도구, 3 보조 프로그램이 덮는 데스크톱 프로그램
문항 종류 type: choice 선택형, short 단답형, fill 완성형, essay_short 서술형, essay_long 논술형
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .util import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20))  # professor | student
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    professor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(100), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    attachment_name: Mapped[str] = mapped_column(String(255), default="")
    attachment_path: Mapped[str] = mapped_column(String(500), default="")
    deadline: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    join_code: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | open | closed
    task_kind: Mapped[str] = mapped_column(String(20), default="performance")  # performance 수행형 | essay 논술형
    tool_slot: Mapped[int] = mapped_column(Integer, default=0)
    tool_name: Mapped[str] = mapped_column(String(50), default="editor")  # editor | sql | google_docs | companion
    tool_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pre_time_limit_min: Mapped[int] = mapped_column(Integer, default=30)
    post_time_limit_min: Mapped[int] = mapped_column(Integer, default=30)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    competencies: Mapped[list[Competency]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan", order_by="Competency.order"
    )
    items: Mapped[list[TestItem]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan", order_by="TestItem.order"
    )
    materials: Mapped[list[Material]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )
    rules: Mapped[list[AIRule]] = relationship(
        back_populates="assignment", cascade="all, delete-orphan"
    )
    enrollments: Mapped[list[Enrollment]] = relationship(back_populates="assignment")
    professor: Mapped[User] = relationship(foreign_keys=[professor_id])


class Competency(Base):
    __tablename__ = "competencies"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    pass_score: Mapped[int] = mapped_column(Integer, default=60)
    grading_criteria: Mapped[str] = mapped_column(Text, default="")

    assignment: Mapped[Assignment] = relationship(back_populates="competencies")


class TestItem(Base):
    __tablename__ = "test_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id"), index=True)
    phase: Mapped[str] = mapped_column(String(10))  # pre | post
    order: Mapped[int] = mapped_column(Integer, default=0)
    type: Mapped[str] = mapped_column(String(20))
    stem: Mapped[str] = mapped_column(Text)
    options: Mapped[list[str]] = mapped_column(JSON, default=list)
    answer: Mapped[str] = mapped_column(Text, default="")

    assignment: Mapped[Assignment] = relationship(back_populates="items")
    competency: Mapped[Competency] = relationship()


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(500))
    mime: Mapped[str] = mapped_column(String(100), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    extracted_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assignment: Mapped[Assignment] = relationship(back_populates="materials")


class AIRule(Base):
    __tablename__ = "ai_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    target: Mapped[str] = mapped_column(String(10))  # ai | student | both
    text: Mapped[str] = mapped_column(Text)

    assignment: Mapped[Assignment] = relationship(back_populates="rules")


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("assignment_id", "student_id", name="uq_enrollment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignments.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20), default="pre")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assignment: Mapped[Assignment] = relationship(back_populates="enrollments")
    student: Mapped[User] = relationship()
    competency_states: Mapped[list[CompetencyState]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan"
    )
    test_attempts: Mapped[list[TestAttempt]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan"
    )
    task_attempts: Mapped[list[TaskAttempt]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", order_by="TaskAttempt.attempt_no"
    )
    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", order_by="ChatMessage.id"
    )
    checks: Mapped[list[LearningCheck]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", order_by="LearningCheck.id"
    )
    summaries: Mapped[list[AISummary]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", order_by="AISummary.id"
    )
    signals: Mapped[list[Signal]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", order_by="Signal.id"
    )
    grade: Mapped[ProfessorGrade | None] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan", uselist=False
    )


class CompetencyState(Base):
    __tablename__ = "competency_states"
    __table_args__ = (UniqueConstraint("enrollment_id", "competency_id", name="uq_state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id"))
    pre_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)

    enrollment: Mapped[Enrollment] = relationship(back_populates="competency_states")
    competency: Mapped[Competency] = relationship()


class TestAttempt(Base):
    __tablename__ = "test_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    phase: Mapped[str] = mapped_column(String(10))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    total_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    competency_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    enrollment: Mapped[Enrollment] = relationship(back_populates="test_attempts")
    answers: Mapped[list[TestAnswer]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="TestAnswer.id"
    )


class TestAnswer(Base):
    __tablename__ = "test_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("test_attempts.id"), index=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("test_items.id"))
    answer_text: Mapped[str] = mapped_column(Text, default="")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    graded_by: Mapped[str] = mapped_column(String(20), default="")  # auto | ai | professor
    # 학생에게 보여 줄 채점 근거와 틀린 부분. {"basis": [{"source", "text"}], "wrong_spans": [문장]} 형태다
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    attempt: Mapped[TestAttempt] = relationship(back_populates="answers")
    item: Mapped[TestItem] = relationship()


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    stage: Mapped[str] = mapped_column(String(20))  # learning | task
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(10))  # student | ai
    content: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20), default="")  # direct | interrogative | refuse
    question_kind: Mapped[str] = mapped_column(String(20), default="")  # reference | reasoning | other
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    enrollment: Mapped[Enrollment] = relationship(back_populates="messages")


class LearningCheck(Base):
    __tablename__ = "learning_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    competency_id: Mapped[int] = mapped_column(ForeignKey("competencies.id"))
    item: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    answer_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feedback: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    enrollment: Mapped[Enrollment] = relationship(back_populates="checks")
    competency: Mapped[Competency] = relationship()


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("enrollment_id", "attempt_no", name="uq_task_attempt"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="in_progress")  # in_progress | submitted
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    enrollment: Mapped[Enrollment] = relationship(back_populates="task_attempts")
    versions: Mapped[list[ArtifactVersion]] = relationship(
        back_populates="attempt", cascade="all, delete-orphan", order_by="ArtifactVersion.version_no"
    )


class ArtifactVersion(Base):
    __tablename__ = "artifact_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("task_attempts.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))  # editor | embed | google | companion
    kind: Mapped[str] = mapped_column(String(20))  # text | image | file
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    attempt: Mapped[TaskAttempt] = relationship(back_populates="versions")


class AISummary(Base):
    __tablename__ = "ai_summaries"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # learning | task
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    enrollment: Mapped[Enrollment] = relationship(back_populates="summaries")


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    enrollment: Mapped[Enrollment] = relationship(back_populates="signals")


class ProfessorGrade(Base):
    __tablename__ = "professor_grades"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), unique=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    enrollment: Mapped[Enrollment] = relationship(back_populates="grade")


class CompanionToken(Base):
    __tablename__ = "companion_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    enrollment: Mapped[Enrollment] = relationship()


class GoogleConnection(Base):
    __tablename__ = "google_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), unique=True)
    doc_id: Mapped[str] = mapped_column(String(100), default="")
    doc_name: Mapped[str] = mapped_column(String(255), default="", server_default="")
    account_email: Mapped[str] = mapped_column(String(255), default="", server_default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_token: Mapped[str] = mapped_column(Text, default="")
    refresh_token: Mapped[str] = mapped_column(Text, default="")
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_revision_id: Mapped[str] = mapped_column(String(100), default="")
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    enrollment: Mapped[Enrollment] = relationship()


class AICallLog(Base):
    """AI 호출 기록. 비용과 검증 기록에 쓴다."""

    __tablename__ = "ai_call_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    purpose: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(50))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
