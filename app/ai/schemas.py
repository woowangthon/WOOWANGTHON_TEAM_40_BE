"""AI 구조화 출력 스키마. Claude API의 output_config.format으로 강제된다.

구조화 출력이 지원하는 JSON 스키마 범위를 지키기 위해 숫자 범위 제약은 두지 않고 코드에서 자른다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class SocraticReply(BaseModel):
    question_kind: Literal["reference", "reasoning", "other"]
    mode: Literal["direct", "interrogative", "refuse"]
    uses_materials: bool
    uncertain: bool
    leaks_answer_self_check: bool
    content: str


class LeakJudgement(BaseModel):
    leaks_answer: bool
    reason: str


class RefinedReply(BaseModel):
    content: str


class CompetencyDraft(BaseModel):
    name: str
    description: str
    grading_criteria: str


class CompetencyDraftList(BaseModel):
    competencies: list[CompetencyDraft]


class ItemDraft(BaseModel):
    competency_name: str
    type: Literal["choice", "short", "fill", "essay_short", "essay_long"]
    stem: str
    options: list[str]
    answer: str


class ItemDraftList(BaseModel):
    items: list[ItemDraft]


class GradeResult(BaseModel):
    score: int
    rationale: str
    feedback: str


class CompetencySummary(BaseModel):
    competency_name: str
    status: Literal["sufficient", "in_progress", "insufficient"]
    line: str


class ProcessSummary(BaseModel):
    spec_summary: str
    learned_summary: str
    ai_usage_line: str
    competencies: list[CompetencySummary]


class SignalItem(BaseModel):
    kind: Literal["non_sequential", "logic_gap", "style_shift", "other"]
    description: str
    evidence: str


class SignalList(BaseModel):
    signals: list[SignalItem]
