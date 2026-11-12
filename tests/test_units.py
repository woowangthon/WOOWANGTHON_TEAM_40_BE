from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.ai.retrieval import build_context, chunk_text, tokenize
from app.routers.google import make_state, parse_doc_id, read_state
from app.util import answer_matches, normalize_answer


def test_google_doc_id_and_state() -> None:
    assert parse_doc_id("https://docs.google.com/document/d/1AbC_def-GHI123456789/edit#heading=h.1") == "1AbC_def-GHI123456789"
    with pytest.raises(HTTPException):
        parse_doc_id("https://example.com/not-a-doc")
    assert read_state(make_state(7, 3)) == (7, 3)
    with pytest.raises(HTTPException):
        read_state("broken")


def test_answer_matching() -> None:
    assert answer_matches("SUM|합계", " sum ")
    assert answer_matches("SUM|합계", "합 계")
    assert not answer_matches("SUM", "AVG")
    assert not answer_matches("SUM", "")
    assert answer_matches("a || b", "a || b")
    # 선택형은 보기 전체가 같을 때만 맞다
    assert answer_matches("a | b", "a | b", alternatives=False)
    assert not answer_matches("a | b", "a", alternatives=False)
    assert normalize_answer("Hello World.") == "helloworld"


def test_tokenize_korean_bigrams() -> None:
    tokens = tokenize("데이터베이스 join")
    assert "데이터베이스" in tokens and "데이" in tokens and "join" in tokens


def test_chunk_and_context_small_material_included_whole() -> None:
    text = "첫째 문단.\n" * 20
    chunks = chunk_text(text, size=50, overlap=10)
    assert len(chunks) > 1
    ctx, has = build_context([("a.txt", text)], "문단", max_chars=10000)
    assert has and ctx.startswith("[자료: a.txt]")


def test_context_selects_relevant_chunks_when_large() -> None:
    relevant = "집계 함수 SUM은 합계를 구한다. GROUP BY와 함께 쓴다. " * 40
    noise = "오늘 날씨는 맑고 바람이 분다. " * 400
    ctx, has = build_context([("noise.txt", noise), ("sql.txt", relevant)], "SUM 집계 함수", max_chars=3000)
    assert has
    assert "sql.txt" in ctx
    assert len(ctx) <= 3600
