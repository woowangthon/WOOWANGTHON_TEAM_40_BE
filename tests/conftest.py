"""테스트는 임시 SQLite와 임시 미디어 폴더를 쓰고 AI 호출은 가짜로 바꾼다."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_tmp = Path(tempfile.mkdtemp(prefix="woowang-test-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["MEDIA_DIR"] = str(_tmp / "media")
os.environ["AI_PROVIDER"] = "gemini"
os.environ["GEMINI_API_KEY"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-for-hs256-0123456789"

from fastapi.testclient import TestClient  # noqa: E402

from app.ai import schemas as ai_schemas  # noqa: E402
from app.ai.client import ai_client  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client() -> Iterator[TestClient]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestClient(app) as c:
        yield c


class FakeAI:
    """output_model에 따라 그럴듯한 값을 돌려준다. 호출 기록을 남긴다."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.grade_score = 85
        self.wrong_spans: list[str] = []
        self.leaks = False

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        model = kwargs["output_model"]
        if model is ai_schemas.SocraticReply:
            return ai_schemas.SocraticReply(
                question_kind="reasoning",
                mode="interrogative",
                uses_materials=True,
                uncertain=False,
                leaks_answer_self_check=False,
                content="지금까지 떠올린 조건은 무엇인가요?",
            )
        if model is ai_schemas.LeakJudgement:
            return ai_schemas.LeakJudgement(leaks_answer=self.leaks, reason="판정 근거")
        if model is ai_schemas.RefinedReply:
            return ai_schemas.RefinedReply(content="다시 쓴 질문입니다. 어떤 열이 필요할까요?")
        if model is ai_schemas.GradeResult:
            return ai_schemas.GradeResult(
                score=self.grade_score,
                rationale="근거",
                feedback="피드백",
                criteria_basis="채점 기준 근거",
                material_basis="학습 자료 근거",
                wrong_spans=self.wrong_spans,
            )
        if model is ai_schemas.ItemDraft:
            return ai_schemas.ItemDraft(
                competency_name="", type="essay_short", stem="확인 문항입니다", options=[], answer="모범 답안"
            )
        if model is ai_schemas.ItemDraftList:
            return ai_schemas.ItemDraftList(
                items=[
                    ai_schemas.ItemDraft(
                        competency_name="조인 이해", type="choice", stem="조인이란", options=["가", "나"], answer="가"
                    ),
                    ai_schemas.ItemDraft(
                        competency_name="집계 함수", type="short", stem="합계 함수는", options=[], answer="SUM"
                    ),
                ]
            )
        if model is ai_schemas.CompetencyDraftList:
            return ai_schemas.CompetencyDraftList(
                competencies=[
                    ai_schemas.CompetencyDraft(name="조인 이해", description="설명", grading_criteria="기준"),
                    ai_schemas.CompetencyDraft(name="집계 함수", description="설명", grading_criteria="기준"),
                ]
            )
        if model is ai_schemas.ProcessSummary:
            return ai_schemas.ProcessSummary(
                spec_summary="스펙",
                learned_summary="학습",
                ai_usage_line="AI를 질문 위주로 썼다",
                competencies=[
                    ai_schemas.CompetencySummary(competency_name="조인 이해", status="sufficient", line="좋음")
                ],
            )
        if model is ai_schemas.SignalList:
            return ai_schemas.SignalList(
                signals=[
                    ai_schemas.SignalItem(kind="non_sequential", description="한 번에 완성", evidence="버전 2")
                ]
            )
        raise AssertionError(f"알 수 없는 출력 모델 {model}")


@pytest.fixture()
def fake_ai(monkeypatch: pytest.MonkeyPatch) -> FakeAI:
    fake = FakeAI()
    monkeypatch.setattr(ai_client, "parse", fake.parse)
    monkeypatch.setattr(type(ai_client), "available", property(lambda self: True))
    return fake
