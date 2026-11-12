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
