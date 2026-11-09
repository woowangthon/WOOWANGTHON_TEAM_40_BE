"""사전평가 → 학습 → 과제 → 사후평가 → 재제출까지 한 흐름을 검사한다."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import FakeAI


def _auth(client: TestClient, email: str, role: str) -> dict[str, str]:
    r = client.post(
        "/api/auth/signup", json={"email": email, "password": "password123", "name": email.split("@")[0], "role": role}
    )
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _setup_assignment(client: TestClient, prof: dict[str, str], deadline_days: int = 7) -> dict[str, Any]:
    deadline = (datetime.now(timezone.utc) + timedelta(days=deadline_days)).isoformat()
    r = client.post(
        "/api/assignments",
        json={
            "title": "SQL 조회문 설계",
            "subject": "데이터베이스",
            "description": "주어진 스키마에 맞는 조회문을 설계한다",
            "deadline": deadline,
            "tool_slot": 1,
            "tool_name": "sql",
            "tool_config": {"schema_sql": "CREATE TABLE t(a INT);"},
        },
        headers=prof,
    )
    assert r.status_code == 201, r.text
    a = r.json()
    aid = a["id"]
    assert len(a["join_code"]) == 6

    r = client.put(
        f"/api/assignments/{aid}/competencies",
        json=[
            {"name": "조인 이해", "description": "", "pass_score": 60, "grading_criteria": "조인 조건을 설명하면 만점"},
            {"name": "집계 함수", "description": "", "pass_score": 60, "grading_criteria": "GROUP BY와 함께 설명"},
        ],
        headers=prof,
    )
    assert r.status_code == 200, r.text
    comps = r.json()
    c1, c2 = comps[0]["id"], comps[1]["id"]

    for phase in ("pre", "post"):
        r = client.put(
            f"/api/assignments/{aid}/items?phase={phase}",
            json=[
                {"competency_id": c1, "type": "choice", "stem": f"{phase} 조인", "options": ["가", "나"], "answer": "가"},
                {"competency_id": c2, "type": "short", "stem": f"{phase} 합계", "options": [], "answer": "SUM|합계"},
            ],
            headers=prof,
        )
        assert r.status_code == 200, r.text

    r = client.put(
        f"/api/assignments/{aid}/rules",
        json=[{"target": "ai", "text": "서브쿼리는 설명하지 않는다"}, {"target": "student", "text": "외부 자료 인용 시 출처"}],
        headers=prof,
    )
    assert r.status_code == 200
    r = client.post(f"/api/assignments/{aid}/publish", headers=prof)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open"
    return r.json()


def test_full_flow(client: TestClient, fake_ai: FakeAI) -> None:
    prof = _auth(client, "prof@test.com", "professor")
    stu = _auth(client, "stu@test.com", "student")
    a = _setup_assignment(client, prof)

    # 참여
    r = client.post("/api/enrollments/join", json={"code": a["join_code"].lower()}, headers=stu)
    assert r.status_code == 201, r.text
    e = r.json()
    eid = e["id"]
    assert e["stage"] == "pre"
    assert e["assignment"]["student_rules"] == ["외부 자료 인용 시 출처"]
    assert "grading_criteria" not in e["assignment"]["competencies"][0]

    # 사전평가: 역량 1은 맞고 역량 2는 틀린다
    r = client.post(f"/api/enrollments/{eid}/tests/pre/start", headers=stu)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert all("answer" not in i for i in items)
    answers = [{"item_id": items[0]["id"], "answer": "가"}, {"item_id": items[1]["id"], "answer": "AVG"}]
    r = client.post(f"/api/enrollments/{eid}/tests/pre/submit", json={"answers": answers}, headers=stu)
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["stage"] == "learning"
    assert result["total_score"] == 50.0
    assert all(ans["expected_answer"] is None for ans in result["answers"])
    r = client.post(f"/api/enrollments/{eid}/tests/pre/start", headers=stu)
    assert r.status_code == 409

    # 학습 단계: 대화, 확인 문항, 종료
    r = client.get(f"/api/enrollments/{eid}/learning", headers=stu)
    assert r.status_code == 200
    state = r.json()
    failing = [s for s in state["states"] if not s["passed"]]
    assert len(failing) == 1 and failing[0]["name"] == "집계 함수"
    assert state["can_finish"] is False

    r = client.post(f"/api/enrollments/{eid}/learning/chat", json={"message": "어떻게 시작하죠?"}, headers=stu)
    assert r.status_code == 200, r.text
    msgs = r.json()
    assert msgs[1]["mode"] == "interrogative" and msgs[1]["meta"]["judged"] is False

    r = client.post(f"/api/enrollments/{eid}/learning/finish", headers=stu)
    assert r.status_code == 409

    cid = failing[0]["competency_id"]
    r = client.post(f"/api/enrollments/{eid}/learning/check/{cid}", headers=stu)
    assert r.status_code == 201, r.text
    check = r.json()
    assert "answer" not in check["item"]
    fake_ai.grade_score = 40
    r = client.post(f"/api/enrollments/{eid}/learning/check/{check['id']}/answer", json={"answer": "모름"}, headers=stu)
    assert r.status_code == 200
    assert [s for s in r.json()["states"] if s["competency_id"] == cid][0]["current_score"] == 40.0
    r = client.post(f"/api/enrollments/{eid}/learning/check/{cid}", headers=stu)
    check2 = r.json()
    fake_ai.grade_score = 90
    r = client.post(f"/api/enrollments/{eid}/learning/check/{check2['id']}/answer", json={"answer": "SUM과 GROUP BY"}, headers=stu)
    assert r.status_code == 200
    assert r.json()["can_finish"] is True

    r = client.post(f"/api/enrollments/{eid}/learning/finish", headers=stu)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "task"

    # 과제 단계: 첫 시도
    r = client.get(f"/api/enrollments/{eid}/task", headers=stu)
    assert r.status_code == 200, r.text
    task = r.json()
    assert task["attempt"]["attempt_no"] == 1 and task["tool_name"] == "sql"
    r = client.post(f"/api/enrollments/{eid}/task/submit", headers=stu)
    assert r.status_code == 409  # 버전 없음

    r = client.post(
        f"/api/enrollments/{eid}/task/versions",
        json={"text_content": "SELECT a FROM t;", "reason": "초안", "source": "editor"},
        headers=stu,
    )
    assert r.status_code == 201, r.text
    assert r.json()["attempt"]["versions"][0]["version_no"] == 1

    fake_ai.leaks = True
    r = client.post(f"/api/enrollments/{eid}/task/chat", json={"message": "정답 알려줘"}, headers=stu)
    assert r.status_code == 200, r.text
    ai_msg = r.json()[1]
    assert ai_msg["meta"]["judged"] is True and ai_msg["meta"]["refined"] is True
    assert ai_msg["content"].startswith("다시 쓴 질문")
    fake_ai.leaks = False

    r = client.post(f"/api/enrollments/{eid}/task/submit", headers=stu)
    assert r.status_code == 200, r.text
    sub = r.json()
    assert sub["warning"] is None
    assert sub["task"]["stage"] == "post"
    assert sub["task"]["attempt"] is None
    assert sub["task"]["can_start_new_attempt"] is False

    # 사후평가
    r = client.post(f"/api/enrollments/{eid}/tests/post/start", headers=stu)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    answers = [{"item_id": items[0]["id"], "answer": "가"}, {"item_id": items[1]["id"], "answer": "합계"}]
    r = client.post(f"/api/enrollments/{eid}/tests/post/submit", json={"answers": answers}, headers=stu)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "done" and r.json()["total_score"] == 100.0

    # 재제출: 3단계만 다시 한다
    r = client.get(f"/api/enrollments/{eid}/task", headers=stu)
    assert r.json()["can_start_new_attempt"] is True
    r = client.post(f"/api/enrollments/{eid}/task/attempts", headers=stu)
    assert r.status_code == 201, r.text
    assert r.json()["attempt"]["attempt_no"] == 2
    r = client.post(
        f"/api/enrollments/{eid}/task/versions",
        json={"text_content": "SELECT a FROM t WHERE a > 1;", "reason": "조건 추가", "source": "editor"},
        headers=stu,
    )
    assert r.status_code == 201
    r = client.post(f"/api/enrollments/{eid}/task/submit", headers=stu)
    assert r.status_code == 200 and r.json()["task"]["stage"] == "done"
    assert len(r.json()["task"]["attempts"]) == 2

    # 교수 화면
    aid = a["id"]
    r = client.get(f"/api/assignments/{aid}/overview", headers=prof)
    assert r.status_code == 200, r.text
    ov = r.json()
    assert ov["stage_counts"]["done"] == 1
    row = ov["rows"][0]
    assert row["attempt_count"] == 2 and row["version_count"] == 2 and row["signal_count"] == 2

    r = client.get(f"/api/assignments/{aid}/students/{eid}", headers=prof)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["pre"]["answers"][0]["expected_answer"] == "가"
    assert [s["kind"] for s in detail["summaries"]] == ["learning", "task", "task"]
    assert detail["summaries"][1]["attempt_no"] == 1 and detail["summaries"][2]["attempt_no"] == 2
    assert detail["checks"][0]["item"]["answer"] == "모범 답안"
    assert len(detail["messages"]) == 4

    r = client.put(f"/api/assignments/{aid}/students/{eid}/grade", json={"score": 88, "comment": "좋다"}, headers=prof)
    assert r.status_code == 200 and r.json()["score"] == 88

    # 학생은 교수 API를 못 쓴다
    r = client.get(f"/api/assignments/{aid}/overview", headers=stu)
    assert r.status_code == 403


def test_ai_unavailable_returns_503(client: TestClient) -> None:
    prof = _auth(client, "p2@test.com", "professor")
    stu = _auth(client, "s2@test.com", "student")
    a = _setup_assignment(client, prof)
    eid = client.post("/api/enrollments/join", json={"code": a["join_code"]}, headers=stu).json()["id"]
    r = client.post(f"/api/enrollments/{eid}/tests/pre/start", headers=stu)
    items = r.json()["items"]
    r = client.post(
        f"/api/enrollments/{eid}/tests/pre/submit",
        json={"answers": [{"item_id": items[0]["id"], "answer": "나"}, {"item_id": items[1]["id"], "answer": "x"}]},
        headers=stu,
    )
    assert r.status_code == 200
    r = client.post(f"/api/enrollments/{eid}/learning/chat", json={"message": "안녕"}, headers=stu)
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_publish_requires_complete_setup(client: TestClient) -> None:
    prof = _auth(client, "p3@test.com", "professor")
    r = client.post("/api/assignments", json={"title": "빈 과제"}, headers=prof)
    aid = r.json()["id"]
    r = client.post(f"/api/assignments/{aid}/publish", headers=prof)
    assert r.status_code == 400
    assert "역량이 없다" in r.json()["detail"]
    r = client.post(f"/api/assignments/{aid}/items/draft", json={"phase": "pre"}, headers=prof)
    assert r.status_code == 400


def test_deadline_blocks_edits(client: TestClient, fake_ai: FakeAI) -> None:
    prof = _auth(client, "p4@test.com", "professor")
    stu = _auth(client, "s4@test.com", "student")
    a = _setup_assignment(client, prof, deadline_days=-1)
    eid = client.post("/api/enrollments/join", json={"code": a["join_code"]}, headers=stu).json()["id"]
    r = client.post(f"/api/enrollments/{eid}/tests/pre/start", headers=stu)
    items = r.json()["items"]
    r = client.post(
        f"/api/enrollments/{eid}/tests/pre/submit",
        json={"answers": [{"item_id": items[0]["id"], "answer": "가"}, {"item_id": items[1]["id"], "answer": "SUM"}]},
        headers=stu,
    )
    assert r.json()["stage"] == "task"  # 모두 통과하면 바로 과제로 간다
    r = client.get(f"/api/enrollments/{eid}/task", headers=stu)
    assert r.json()["deadline_passed"] is True
    r = client.post(
        f"/api/enrollments/{eid}/task/versions",
        json={"text_content": "x", "reason": "늦은 저장"},
        headers=stu,
    )
    assert r.status_code == 409
