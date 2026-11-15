"""보고서(docx): 텍스트 추출, 편집기 설정 서명, 저장 콜백 커밋."""

from __future__ import annotations

import io
from typing import Any

import jwt
import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import onlyoffice
from app.workspace.docx_store import blank_docx, extract_text
from tests.test_workspace import _setup


def _docx(*lines: str) -> bytes:
    d = Document()
    for line in lines:
        d.add_paragraph(line)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "q=2", "12ms"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_extract_text() -> None:
    assert extract_text(blank_docx("# 개요\n본문")) == "개요\n본문"
    assert extract_text(_docx("첫 문단", "둘째 문단")) == "첫 문단\n둘째 문단\nq=2 | 12ms"


def test_doc_config_and_callback(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onlyoffice_jwt_secret", "oo-secret-for-tests-0123456789abcdef")
    h, eid = _setup(client)
    base = f"/api/enrollments/{eid}/workspace"
    client.get(base, headers=h)

    r = client.get(f"{base}/doc-config", params={"path": "report.docx"}, headers=h)
    assert r.status_code == 200, r.text
    cfg = r.json()["config"]
    signed = jwt.decode(cfg["token"], settings.onlyoffice_jwt_secret, algorithms=["HS256"])
    assert signed["document"]["key"] == cfg["document"]["key"]
    # Document Server 가 쓰는 다운로드 주소는 학생 로그인 없이 파일 토큰으로 열린다
    url = cfg["document"]["url"].split("8000", 1)[1]
    assert client.get(url).status_code == 200
    assert client.get(url.split("?")[0] + "?t=bad").status_code == 403
    assert client.post(f"{base}/commit", json={"path": "report.docx", "content": "x"}, headers=h).status_code == 400

    # 저장 콜백: 저장본을 받아 텍스트가 바뀌었으면 커밋한다
    saved = _docx("Round Robin 은 큐를 순환한다")

    class FakeResp:
        status_code = 200
        content = saved

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...
        async def __aenter__(self) -> "FakeClient":
            return self
        async def __aexit__(self, *_: Any) -> None: ...
        async def get(self, _url: str) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(onlyoffice.httpx, "AsyncClient", FakeClient)
    cb = cfg["editorConfig"]["callbackUrl"].split("8000", 1)[1]
    payload = {"status": 6, "url": "http://localhost:8080/cache/x.docx", "key": cfg["document"]["key"], "userdata": "focus_ai"}
    body = {**payload, "token": jwt.encode(payload, settings.onlyoffice_jwt_secret, algorithm="HS256")}
    assert client.post(cb, json=body).json() == {"error": 0}
    c = client.get(base, headers=h).json()["commits"][0]
    assert (c["file"], c["author"], c["trigger"]) == ("report.docx", "human", "focus_ai")
    assert any(d["type"] == "add" and "순환" in d["text"] for d in c["diff"])
    # 서명이 틀린 콜백은 받지 않는다
    bad = {**payload, "token": jwt.encode(payload, "wrong-secret-0123456789abcdef0123", algorithm="HS256")}
    assert client.post(cb, json=bad).json() == {"error": 1}
    # 세션이 끝난 저장(status 2)은 문서 키를 바꾼다
    p2 = {**payload, "status": 2, "userdata": ""}  # key 는 지금 키와 같다
    client.post(cb, json={**p2, "token": jwt.encode(p2, settings.onlyoffice_jwt_secret, algorithm="HS256")})
    cfg2 = client.get(f"{base}/doc-config", params={"path": "report.docx"}, headers=h).json()["config"]
    assert cfg2["document"]["key"] != cfg["document"]["key"]


def test_stale_session_close_does_not_bump_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "onlyoffice_jwt_secret", "oo-secret-for-tests-0123456789abcdef")
    h, eid = _setup(client)
    base = f"/api/enrollments/{eid}/workspace"
    client.get(base, headers=h)
    cfg = client.get(f"{base}/doc-config", params={"path": "report.docx"}, headers=h).json()["config"]

    class FakeResp:
        status_code = 200
        content = _docx("x")

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...
        async def __aenter__(self) -> "FakeClient":
            return self
        async def __aexit__(self, *_: Any) -> None: ...
        async def get(self, _url: str) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(onlyoffice.httpx, "AsyncClient", FakeClient)
    cb = cfg["editorConfig"]["callbackUrl"].split("8000", 1)[1]
    stale = {"status": 2, "url": "http://localhost:8080/c.docx", "key": "ws999r0"}
    client.post(cb, json={**stale, "token": jwt.encode(stale, settings.onlyoffice_jwt_secret, algorithm="HS256")})
    again = client.get(f"{base}/doc-config", params={"path": "report.docx"}, headers=h).json()["config"]
    assert again["document"]["key"] == cfg["document"]["key"]


def _styled() -> bytes:
    d = Document()
    d.add_heading("개요", level=1)
    p = d.add_paragraph()
    r = p.add_run("굵은 첫 문단")
    r.bold = True
    p.add_run(" 이어짐")
    d.add_paragraph("둘째 문단")
    t = d.add_table(rows=1, cols=1)
    t.rows[0].cells[0].text = "표"
    d.add_paragraph("마지막")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_apply_lines_keeps_style_and_blocks_tables() -> None:
    from app.workspace.docx_store import DocEditError, apply_lines, locked_lines

    data = _styled()
    assert extract_text(data) == "개요\n굵은 첫 문단 이어짐\n둘째 문단\n표\n마지막"
    assert locked_lines(data) == []  # 표 행도 칸이 단순하면 고칠 수 있다
    # 한 문단을 두 문단으로: 첫 글자 모양(굵게)을 이어받는다
    out = apply_lines(data, 2, 2, "새 첫 문단\n덧붙인 문단")
    assert extract_text(out) == "개요\n새 첫 문단\n덧붙인 문단\n둘째 문단\n표\n마지막"
    paras = Document(io.BytesIO(out)).paragraphs
    assert paras[1].runs[0].bold and paras[2].runs[0].bold
    # 제목 바로 뒤에 넣으면 제목 모양이 아니라 본문 모양이다
    out = apply_lines(data, 2, 1, "끼운 문단")
    p = Document(io.BytesIO(out)).paragraphs[1]
    assert p.text == "끼운 문단" and not str(p.style.name if p.style else "").startswith("Heading")
    # 지우기, 맨 끝에 붙이기
    assert extract_text(apply_lines(data, 3, 3, "")) == "개요\n굵은 첫 문단 이어짐\n표\n마지막"
    assert extract_text(apply_lines(data, 6, 5, "끝")).endswith("마지막\n끝")
    with pytest.raises(DocEditError):
        apply_lines(data, 3, 4, "x\ny")  # 문단과 표 행을 한 번에 바꾸지 않는다
    # 지우기는 문단과 표가 섞여도 된다. 표의 모든 행이 들어 있으면 표가 없어진다
    out = apply_lines(data, 3, 4, "")
    assert extract_text(out) == "개요\n굵은 첫 문단 이어짐\n마지막" and not Document(io.BytesIO(out)).tables
    with pytest.raises(DocEditError):
        apply_lines(data, 9, 9, "x")


def test_ai_edit_on_report(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workspace.ai import WorkspaceReply

    monkeypatch.setattr(settings, "onlyoffice_jwt_secret", "oo-secret-for-tests-0123456789abcdef")
    h, eid = _setup(client)
    base = f"/api/enrollments/{eid}/workspace"
    client.get(base, headers=h)
    cfg = client.get(f"{base}/doc-config", params={"path": "report.docx"}, headers=h).json()["config"]

    # 학생이 쓴 보고서가 콜백으로 들어와 있다
    saved = _docx("서론", "본문 초안")

    class FakeResp:
        status_code = 200
        content = saved

    class FakeClient:
        def __init__(self, **_: Any) -> None: ...
        async def __aenter__(self) -> "FakeClient":
            return self
        async def __aexit__(self, *_: Any) -> None: ...
        async def get(self, _url: str) -> FakeResp:
            return FakeResp()

    monkeypatch.setattr(onlyoffice.httpx, "AsyncClient", FakeClient)
    cb = cfg["editorConfig"]["callbackUrl"].split("8000", 1)[1]
    old_key = cfg["document"]["key"]

    def post(status: int, key: str) -> None:
        payload = {"status": status, "url": "http://localhost:8080/c.docx", "key": key}
        client.post(cb, json={**payload, "token": jwt.encode(payload, settings.onlyoffice_jwt_secret, algorithm="HS256")})

    post(6, old_key)
    assert client.get(base, headers=h).json()["files"][2]["content"] == "서론\n본문 초안\nq=2 | 12ms"

    # 표 행을 건드리는 수정안은 만들지 않는다
    monkeypatch.setattr(
        "app.workspace.ai.ai_client.parse",
        lambda **_: WorkspaceReply(content="표", has_edit=True, edit_path="report.docx", start_line=3, end_line=3, new_text="x", edit_reason="r"),
    )
    r = client.post(f"{base}/chat", json={"message": "표 고쳐", "path": "report.docx"}, headers=h).json()
    assert r["proposal"] is None and "칸이 2개" in r["messages"][1]["content"]

    monkeypatch.setattr(
        "app.workspace.ai.ai_client.parse",
        lambda **_: WorkspaceReply(content="다듬었다", has_edit=True, edit_path="report.docx", start_line=2, end_line=2, new_text="Round Robin 은 큐를 돈다.", edit_reason="본문 다듬기"),
    )
    p = client.post(f"{base}/chat", json={"message": "본문 다듬어 줘", "path": "report.docx"}, headers=h).json()["proposal"]
    assert p and p["old_text"] == "본문 초안"
    acc = client.post(f"{base}/proposals/{p['id']}/accept", json={}, headers=h).json()
    assert acc["commit"]["author"] == "ai" and acc["file"]["content"] == "서론\nRound Robin 은 큐를 돈다.\nq=2 | 12ms"
    assert acc["doc_key"] and acc["doc_key"] != old_key

    # 옛 세션이 닫히며 보내는 저장본은 AI 가 넣은 내용을 덮어쓰지 못한다
    post(2, old_key)
    w = client.get(base, headers=h).json()
    assert w["files"][2]["content"] == "서론\nRound Robin 은 큐를 돈다.\nq=2 | 12ms"
    new_cfg = client.get(f"{base}/doc-config", params={"path": "report.docx"}, headers=h).json()["config"]
    assert new_cfg["document"]["key"] == acc["doc_key"]


def _table_doc(merged: bool = False) -> bytes:
    d = Document()
    d.add_paragraph("결과")
    t = d.add_table(rows=2, cols=3)
    for c, v in zip(t.rows[0].cells, ["q", "평균 대기", "교환"], strict=True):
        c.text = v
    for c, v in zip(t.rows[1].cells, ["2", "12ms", "9"], strict=True):
        c.text = v
    t.rows[0].cells[0].paragraphs[0].runs[0].bold = True
    if merged:
        t.cell(1, 1).merge(t.cell(1, 2))
    d.add_paragraph("끝")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_apply_lines_tables() -> None:
    from app.workspace.docx_store import DocEditError, apply_lines, locked_lines, table_lines

    data = _table_doc()
    assert extract_text(data) == "결과\nq | 평균 대기 | 교환\n2 | 12ms | 9\n끝"
    assert table_lines(data) == [(2, 3, 3)]
    # 칸 글자 바꾸기: 칸 모양(굵게)은 남는다
    out = apply_lines(data, 2, 2, "퀀텀 | 평균 대기 | 문맥 교환")
    assert extract_text(out).split("\n")[1] == "퀀텀 | 평균 대기 | 문맥 교환"
    assert Document(io.BytesIO(out)).tables[0].rows[0].cells[0].paragraphs[0].runs[0].bold
    # 행 더하기: 표 끝 다음 줄에 '칸 | 칸' 을 넣으면 같은 표의 행이 된다
    out = apply_lines(data, 4, 3, "4 | 15ms | 5\n8 | 21ms | 3")
    assert extract_text(out) == "결과\nq | 평균 대기 | 교환\n2 | 12ms | 9\n4 | 15ms | 5\n8 | 21ms | 3\n끝"
    assert len(Document(io.BytesIO(out)).tables[0].rows) == 4
    # 행 바꾸면서 늘리기, 행 지우기
    out = apply_lines(data, 3, 3, "1 | 10ms | 12\n2 | 12ms | 9")
    assert len(Document(io.BytesIO(out)).tables[0].rows) == 3
    assert extract_text(apply_lines(data, 3, 3, "")) == "결과\nq | 평균 대기 | 교환\n끝"
    # 표 뒤에 일반 문단을 넣으면 표 안이 아니라 표 다음에 들어간다
    out = apply_lines(data, 4, 3, "표를 보면 퀀텀이 작을수록 교환이 많다")
    assert extract_text(out).split("\n")[3] == "표를 보면 퀀텀이 작을수록 교환이 많다"
    assert len(Document(io.BytesIO(out)).tables[0].rows) == 2
    # 칸 수가 틀리면, 표 중간에 문단을 넣으면 받지 않는다
    with pytest.raises(DocEditError, match="칸이 3개"):
        apply_lines(data, 3, 3, "2 | 12ms")
    with pytest.raises(DocEditError):
        apply_lines(data, 3, 2, "문단")
    # 병합된 칸이 있는 행은 막는다
    m = _table_doc(merged=True)
    assert 3 in locked_lines(m)
    with pytest.raises(DocEditError):
        apply_lines(m, 3, 3, "2 | 12ms | 9")
    with pytest.raises(DocEditError):
        apply_lines(m, 3, 3, "")  # 병합된 표의 일부 행만 지우기
    # 병합된 표라도 통째로, 앞뒤 문단과 함께 지울 수 있다
    out = apply_lines(m, 1, 4, "")
    assert extract_text(out) == "" and not Document(io.BytesIO(out)).tables


def test_apply_lines_headings_draft() -> None:
    """초안 쓰기: 제목 틀 전체를 바꿔도 '# ' 줄은 제목, 나머지는 본문 모양이 된다."""
    from app.workspace.docx_store import apply_lines, heading_levels

    data = blank_docx("# 1. 개요\n\n# 2. 구현\n\n# 3. 비교")
    assert heading_levels(data) == {1: 1, 3: 1, 5: 1}
    draft = "# 1. 개요\n라운드 로빈 설명.\n둘째 문단.\n# 2. 구현\ndeque 를 쓴다.\n## 2.1 세부\n세부 설명.\n# 3. 비교\n(실행 결과를 넣는다)"
    out = apply_lines(data, 1, 5, draft)
    assert extract_text(out) == draft.replace("## ", "").replace("# ", "")
    levels = heading_levels(out)
    assert levels == {1: 1, 4: 1, 6: 2, 8: 1}
    styles = [p.style.name if p.style else "" for p in Document(io.BytesIO(out)).paragraphs]
    assert styles[1] == styles[2] == styles[4] == "Normal"
