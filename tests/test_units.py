from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.ai.retrieval import build_context, chunk_text, tokenize
from app.routers.google import make_state, parse_doc_id, read_state
from app.util import answer_matches, normalize_answer, static_file


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


def test_static_file_stays_inside_root(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """화면 파일 폴더 밖의 파일은 주소를 어떻게 써도 내주지 않는다."""
    dist = tmp_path / "dist"
    (dist / "login").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "login" / "photo.webp").write_bytes(b"x")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "app.db").write_bytes(b"secret")

    assert static_file(dist, "login/photo.webp") == (dist / "login" / "photo.webp").resolve()
    assert static_file(dist, "") is None
    assert static_file(dist, "login") is None
    assert static_file(dist, "missing.js") is None
    for attack in ("../data/app.db", r"..\data\app.db", "login/../../data/app.db", str(tmp_path / "data" / "app.db")):
        assert static_file(dist, attack) is None, attack
