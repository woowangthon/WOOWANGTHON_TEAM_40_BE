"""자리 2 Google 문서 연동. Google Drive API는 가짜 응답으로 바꿔 문서 전환과 본문 받기를 검사한다."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from app.config import settings
from app.db import SessionLocal, add_missing_columns
from app.models import ArtifactVersion, Assignment, CompanionToken, Enrollment, GoogleConnection, TaskAttempt, User
from app.routers import google
from app.util import utcnow

DOC_A = "1AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DOC_B = "1BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
ACCOUNT = "student.account@example.com"


class FakeDrive:
    """문서별 수정 이력을 들고 Drive API와 같은 모양으로 답한다. 수정 이력 번호는 실제처럼 문서마다 1부터 매긴다."""

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    def add_doc(self, doc_id: str, name: str, revisions: dict[str, str], mime: str = google.DOC_MIME) -> None:
        self.docs[doc_id] = {"name": name, "mime": mime, "revisions": dict(revisions), "can_read_revisions": True}

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(url)
        if url == google.DRIVE_ABOUT:
            return httpx.Response(200, json={"user": {"emailAddress": ACCOUNT}})
        if url.startswith("https://export.test/"):
            # 실제 내보내기 주소처럼 리다이렉트를 따라가야만 본문을 준다
            if not kwargs.get("follow_redirects"):
                return httpx.Response(307, headers={"location": "https://content.test/x"})
            _, _, _, doc_id, rid = url.split("/")
            return httpx.Response(200, text="﻿" + self.docs[doc_id]["revisions"][rid])
        path = url.removeprefix(google.DRIVE_FILES + "/")
        doc_id = path.split("/")[0]
        doc = self.docs.get(doc_id)
        if doc is None:
            body = {"error": {"code": 404, "message": f"File not found: {doc_id}.", "errors": [{"reason": "notFound"}]}}
            return httpx.Response(404, json=body)
        if path.endswith("/revisions"):
            revs = [
                {
                    "id": rid,
                    "modifiedTime": f"2026-11-10T10:00:0{i}.000Z",
                    "exportLinks": {"text/plain": f"https://export.test/{doc_id}/{rid}"},
                }
                for i, rid in enumerate(doc["revisions"])
            ]
            return httpx.Response(200, json={"revisions": revs})
        return httpx.Response(
            200,
            json={
                "id": doc_id,
                "name": doc["name"],
                "mimeType": doc["mime"],
                "capabilities": {"canReadRevisions": doc["can_read_revisions"]},
            },
        )


@pytest.fixture()
def drive(monkeypatch: pytest.MonkeyPatch) -> FakeDrive:
    fake = FakeDrive()
    monkeypatch.setattr(google.httpx, "get", fake.get)
    monkeypatch.setattr(settings, "google_client_id", "test-client-id")
    monkeypatch.setattr(settings, "google_client_secret", "test-client-secret")
    return fake


def _student_on_task(client: TestClient, tool_slot: int = 2, tool_name: str = "google_docs") -> tuple[dict[str, str], int]:
    """과제 단계에 들어와 있고 Google 계정을 연결한 학생을 만든다. 인증 헤더와 참여 ID를 돌려준다."""
    r = client.post(
        "/api/auth/signup", json={"email": "stu@test.com", "password": "password123", "name": "학생", "role": "student"}
    )
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    with SessionLocal() as db:
        student = db.query(User).filter(User.email == "stu@test.com").one()
        prof = User(email="prof@test.com", password_hash="x", name="교수", role="professor")
        db.add(prof)
        db.flush()
        a = Assignment(
            professor_id=prof.id,
            title="정책 제안서",
            join_code="GDOC01",
            status="open",
            tool_slot=tool_slot,
            tool_name=tool_name,
        )
        db.add(a)
        db.flush()
        e = Enrollment(assignment_id=a.id, student_id=student.id, stage="task")
        db.add(e)
        db.flush()
        db.add(
            GoogleConnection(
                enrollment_id=e.id,
                access_token="test-access-token",
                refresh_token="test-refresh-token",
                token_expiry=utcnow() + timedelta(hours=1),
            )
        )
        db.commit()
        eid = e.id
    assert client.get(f"/api/enrollments/{eid}/task", headers=headers).status_code == 200
    return headers, eid


def _set_doc(client: TestClient, headers: dict[str, str], eid: int, doc_id: str) -> Any:
    url = f"https://docs.google.com/document/d/{doc_id}/edit?tab=t.0"
    return client.post(f"/api/enrollments/{eid}/google/doc", json={"doc_url": url}, headers=headers)


def test_parse_doc_id_accepts_other_address_forms() -> None:
    assert google.parse_doc_id(f"https://docs.google.com/document/u/1/d/{DOC_A}/edit") == DOC_A
    assert google.parse_doc_id(f"https://drive.google.com/file/d/{DOC_A}/view?usp=sharing") == DOC_A
    assert google.parse_doc_id(f"https://drive.google.com/open?id={DOC_A}") == DOC_A
    assert google.parse_doc_id(DOC_A) == DOC_A
    for bad in (
        "https://docs.google.com/document/d/e/2PACX-1vTabcdefghijklmnopqrstuvwxyz/pub",
        f"https://docs.google.com/spreadsheets/d/{DOC_A}/edit",
        f"https://example.com/page?id={DOC_A}",
    ):
        with pytest.raises(Exception) as exc:
            google.parse_doc_id(bad)
        assert getattr(exc.value, "status_code", None) == 400


def test_set_doc_saves_revision_text(client: TestClient, drive: FakeDrive) -> None:
    headers, eid = _student_on_task(client)
    drive.add_doc(DOC_A, "제안서 초안", {"1": "서론", "14": "서론\r\n근거 하나"})

    r = _set_doc(client, headers, eid, DOC_A)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    task = body["task"]
    assert task["google_doc_id"] == DOC_A and task["google_doc_name"] == "제안서 초안"
    assert task["google_last_synced_at"] is not None
    versions = task["attempt"]["versions"]
    # 리다이렉트를 따라가 본문을 받고, 맨 앞 BOM과 CRLF를 정리한다
    assert [v["text_content"] for v in versions] == ["서론", "서론\n근거 하나"]
    assert all(v["meta"]["doc_id"] == DOC_A and v["meta"]["doc_name"] == "제안서 초안" for v in versions)

    again = client.post(f"/api/enrollments/{eid}/google/sync", headers=headers)
    assert again.status_code == 200 and again.json()["added"] == 0


def test_switching_doc_keeps_each_doc_history(client: TestClient, drive: FakeDrive) -> None:
    headers, eid = _student_on_task(client)
    drive.add_doc(DOC_A, "첫 문서", {"1": "가", "2": "가나"})
    drive.add_doc(DOC_B, "둘째 문서", {"1": "다", "2": "다라"})
    assert _set_doc(client, headers, eid, DOC_A).json()["added"] == 2

    # 둘째 문서의 수정 이력 번호 1과 2는 첫 문서와 겹치지만 다른 문서이므로 모두 받아야 한다
    r = _set_doc(client, headers, eid, DOC_B)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 2
    task = r.json()["task"]
    assert task["google_doc_id"] == DOC_B and task["google_doc_name"] == "둘째 문서"
    versions = task["attempt"]["versions"]
    assert [(v["version_no"], v["meta"]["doc_id"], v["text_content"]) for v in versions] == [
        (1, DOC_A, "가"),
        (2, DOC_A, "가나"),
        (3, DOC_B, "다"),
        (4, DOC_B, "다라"),
    ]

    # 첫 문서로 돌아가면 그 문서에서 새로 생긴 이력만 받는다
    drive.docs[DOC_A]["revisions"]["3"] = "가나다"
    back = _set_doc(client, headers, eid, DOC_A)
    assert back.json()["added"] == 1
    last = back.json()["task"]["attempt"]["versions"][-1]
    assert (last["version_no"], last["meta"]["doc_id"], last["text_content"]) == (5, DOC_A, "가나다")


def test_unreadable_doc_explains_account_and_keeps_current_doc(client: TestClient, drive: FakeDrive) -> None:
    headers, eid = _student_on_task(client)
    drive.add_doc(DOC_A, "첫 문서", {"1": "가"})
    assert _set_doc(client, headers, eid, DOC_A).status_code == 200

    r = _set_doc(client, headers, eid, DOC_B)
    assert r.status_code == 400
    assert ACCOUNT in r.json()["detail"] and "찾을 수 없다" in r.json()["detail"]
    task = client.get(f"/api/enrollments/{eid}/task", headers=headers).json()
    assert task["google_doc_id"] == DOC_A and task["google_account"] == ACCOUNT

    drive.add_doc(DOC_B, "Word 파일", {}, mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert "Google 문서 형식이 아니다" in _set_doc(client, headers, eid, DOC_B).json()["detail"]

    drive.add_doc(DOC_B, "보기 전용 문서", {"1": "가"})
    drive.docs[DOC_B]["can_read_revisions"] = False
    assert "수정 이력을 읽을 권한이 없다" in _set_doc(client, headers, eid, DOC_B).json()["detail"]


def test_sync_fills_text_missing_from_earlier_versions(client: TestClient, drive: FakeDrive) -> None:
    headers, eid = _student_on_task(client)
    drive.add_doc(DOC_A, "첫 문서", {"1": "가", "2": "가나"})
    with SessionLocal() as db:
        attempt = db.query(TaskAttempt).filter(TaskAttempt.enrollment_id == eid).one()
        conn = db.query(GoogleConnection).filter(GoogleConnection.enrollment_id == eid).one()
        conn.doc_id = DOC_A
        # 본문 없이 저장된 예전 버전
        db.add(
            ArtifactVersion(
                attempt_id=attempt.id,
                version_no=1,
                source="google",
                kind="text",
                text_content=None,
                reason="Google 문서 수정 이력",
                meta={"revision_id": "1", "doc_id": DOC_A},
            )
        )
        db.commit()

    r = client.post(f"/api/enrollments/{eid}/google/sync", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1
    versions = r.json()["task"]["attempt"]["versions"]
    assert [(v["version_no"], v["text_content"]) for v in versions] == [(1, "가"), (2, "가나")]


def test_new_attempt_does_not_reimport_old_revisions(client: TestClient, drive: FakeDrive) -> None:
    headers, eid = _student_on_task(client)
    drive.add_doc(DOC_A, "첫 문서", {"1": "가", "2": "가나"})
    assert _set_doc(client, headers, eid, DOC_A).json()["added"] == 2
    with SessionLocal() as db:
        first = db.query(TaskAttempt).filter(TaskAttempt.enrollment_id == eid).one()
        first.status = "submitted"
        db.add(TaskAttempt(enrollment_id=eid, attempt_no=2))
        db.commit()

    assert client.post(f"/api/enrollments/{eid}/google/sync", headers=headers).json()["added"] == 0
    drive.docs[DOC_A]["revisions"]["3"] = "가나다"
    r = client.post(f"/api/enrollments/{eid}/google/sync", headers=headers)
    assert r.json()["added"] == 1
    second = r.json()["task"]["attempt"]
    assert second["attempt_no"] == 2
    assert [(v["version_no"], v["text_content"]) for v in second["versions"]] == [(1, "가나다")]


def test_task_state_reports_whether_companion_is_running(client: TestClient) -> None:
    headers, eid = _student_on_task(client, tool_slot=3, tool_name="companion")
    task = client.get(f"/api/enrollments/{eid}/task", headers=headers).json()
    assert task["companion_last_seen"] is None and task["companion_live"] is False

    with SessionLocal() as db:
        db.add(CompanionToken(enrollment_id=eid, token="tok-old", last_seen=utcnow() - timedelta(minutes=10)))
        db.commit()
    task = client.get(f"/api/enrollments/{eid}/task", headers=headers).json()
    assert task["companion_last_seen"] is not None and task["companion_live"] is False

    # 보조 프로그램이 서버에 접속하면 돌고 있는 것으로 본다
    assert client.get("/api/companion/me", headers={"X-Companion-Token": "tok-old"}).status_code == 200
    task = client.get(f"/api/enrollments/{eid}/task", headers=headers).json()
    assert task["companion_live"] is True


def test_add_missing_columns_upgrades_existing_table(tmp_path: Any) -> None:
    eng = create_engine(f"sqlite:///{(tmp_path / 'old.db').as_posix()}")
    # 열을 더하기 전의 테이블 모양
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE google_connections (id INTEGER PRIMARY KEY, enrollment_id INTEGER NOT NULL, "
                "doc_id VARCHAR(100) NOT NULL, access_token TEXT NOT NULL, refresh_token TEXT NOT NULL, "
                "token_expiry DATETIME, last_revision_id VARCHAR(100) NOT NULL, connected_at DATETIME NOT NULL)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO google_connections VALUES (1, 1, 'x', 'a', 'r', NULL, '', '2026-11-10 10:00:00')"
            )
        )

    added = add_missing_columns(eng)
    assert {"google_connections.doc_name", "google_connections.account_email", "google_connections.last_synced_at"} <= set(added)
    assert "doc_name" in {c["name"] for c in inspect(eng).get_columns("google_connections")}
    with eng.connect() as conn:
        row = conn.execute(text("SELECT doc_name, account_email, last_synced_at FROM google_connections")).one()
    assert tuple(row) == ("", "", None)
    assert add_missing_columns(eng) == []
    eng.dispose()
