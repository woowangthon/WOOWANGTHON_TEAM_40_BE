"""과제 단계 밖의 화면이 쓰는 API. 과제 현황의 반 전체 지표, 시험 이어 보기, 학생에게 보이는 자료와 교수 평가, 운영 설정 검사."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import DEV_SECRET_KEY, Settings
from tests.conftest import FakeAI
from tests.test_flow import _auth, _setup_assignment


def _join(client: TestClient, headers: dict[str, str], code: str) -> dict[str, Any]:
    r = client.post("/api/enrollments/join", json={"code": code}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _take(client: TestClient, headers: dict[str, str], eid: int, phase: str, first: str, second: str) -> dict[str, Any]:
    """문항 두 개짜리 시험을 본다. 첫 문항은 선택형, 둘째 문항은 단답형이다."""
    started = client.post(f"/api/enrollments/{eid}/tests/{phase}/start", headers=headers)
    assert started.status_code == 200, started.text
    items = started.json()["items"]
    answers = [{"item_id": items[0]["id"], "answer": first}, {"item_id": items[1]["id"], "answer": second}]
    r = client.post(f"/api/enrollments/{eid}/tests/{phase}/submit", json={"answers": answers}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_overview_reports_class_stats(client: TestClient, fake_ai: FakeAI) -> None:
    prof = _auth(client, "prof@test.com", "professor")
    a = _setup_assignment(client, prof)
    aid = a["id"]
    names = [c["name"] for c in a["competencies"]]

    # 아무도 참여하지 않았을 때는 평균이 비어 있다
    empty = client.get(f"/api/assignments/{aid}/overview", headers=prof).json()["stats"]
    assert empty["enrolled"] == 0 and empty["pre_avg"] is None and empty["pre_distribution"] == [0, 0, 0, 0, 0]

    high = _auth(client, "high@test.com", "student")
    low = _auth(client, "low@test.com", "student")
    idle = _auth(client, "idle@test.com", "student")
    e_high = _join(client, high, a["join_code"])["id"]
    e_low = _join(client, low, a["join_code"])["id"]
    _join(client, idle, a["join_code"])

    assert _take(client, high, e_high, "pre", "가", "SUM")["total_score"] == 100
    assert _take(client, low, e_low, "pre", "가", "모름")["total_score"] == 50

    data = client.get(f"/api/assignments/{aid}/overview", headers=prof).json()
    stats = data["stats"]
    assert (stats["enrolled"], stats["pre_taken"], stats["post_taken"]) == (3, 2, 0)
    assert (stats["pre_avg"], stats["pre_min"], stats["pre_max"]) == (75.0, 50.0, 100.0)
    # 50점은 둘째 구간, 100점은 마지막 구간
    assert stats["pre_distribution"] == [0, 1, 0, 0, 1]
    assert (stats["not_started"], stats["learning_now"], stats["learning_done"]) == (1, 1, 1)
    assert stats["gain_avg"] is None and stats["dropped_count"] == 0

    comps = {c["name"]: c for c in stats["competencies"]}
    assert comps[names[0]]["pre_avg"] == 100.0 and comps[names[0]]["below_count"] == 0
    assert comps[names[1]]["pre_avg"] == 50.0 and comps[names[1]]["below_count"] == 1
    assert comps[names[1]]["learning_count"] == 1 and comps[names[1]]["post_avg"] is None

    rows = {r["student_email"]: r for r in data["rows"]}
    assert rows["low@test.com"]["weak_competencies"] == [names[1]]
    assert rows["low@test.com"]["pre_scores"] == [100.0, 0.0]
    assert rows["high@test.com"]["weak_competencies"] == []
    assert rows["idle@test.com"]["pre_scores"] == [None, None] and rows["idle@test.com"]["pre_score"] is None


def test_overview_reports_gain_after_post_test(client: TestClient, fake_ai: FakeAI) -> None:
    prof = _auth(client, "prof@test.com", "professor")
    a = _setup_assignment(client, prof)
    aid = a["id"]
    stu = _auth(client, "stu@test.com", "student")
    eid = _join(client, stu, a["join_code"])["id"]
    _take(client, stu, eid, "pre", "가", "SUM")
    # 과제 화면을 열면 첫 시도가 생긴다
    assert client.get(f"/api/enrollments/{eid}/task", headers=stu).status_code == 200
    r = client.post(
        f"/api/enrollments/{eid}/task/versions",
        json={"text_content": "SELECT 1", "reason": "첫 저장", "source": "embed"},
        headers=stu,
    )
    assert r.status_code == 201, r.text
    assert client.post(f"/api/enrollments/{eid}/task/submit", headers=stu).status_code == 200
    # 사후평가에서 한 문항을 틀려 사전보다 점수가 내려간다
    assert _take(client, stu, eid, "post", "가", "모름")["total_score"] == 50

    data = client.get(f"/api/assignments/{aid}/overview", headers=prof).json()
    stats = data["stats"]
    assert (stats["post_taken"], stats["post_avg"], stats["gain_avg"], stats["dropped_count"]) == (1, 50.0, -50.0, 1)
    assert (stats["submitted_count"], stats["graded_count"]) == (1, 0)
    row = data["rows"][0]
    assert row["gain"] == -50.0 and row["submitted"] is True

    client.put(f"/api/assignments/{aid}/students/{eid}/grade", json={"score": 80, "comment": "근거가 좋다"}, headers=prof)
    assert client.get(f"/api/assignments/{aid}/overview", headers=prof).json()["stats"]["graded_count"] == 1


def test_open_test_attempt_can_be_resumed(client: TestClient, fake_ai: FakeAI) -> None:
    prof = _auth(client, "prof@test.com", "professor")
    a = _setup_assignment(client, prof)
    stu = _auth(client, "stu@test.com", "student")
    eid = _join(client, stu, a["join_code"])["id"]

    # 시작 전에는 이어 볼 시험이 없고, 확인만으로 시험이 시작되지 않는다
    assert client.get(f"/api/enrollments/{eid}/tests/pre/open", headers=stu).status_code == 404
    assert client.get(f"/api/enrollments/{eid}/tests/pre/open", headers=stu).status_code == 404

    started = client.post(f"/api/enrollments/{eid}/tests/pre/start", headers=stu).json()
    resumed = client.get(f"/api/enrollments/{eid}/tests/pre/open", headers=stu)
    assert resumed.status_code == 200
    assert resumed.json()["attempt_id"] == started["attempt_id"]
    assert resumed.json()["started_at"] == started["started_at"]
    assert [i["id"] for i in resumed.json()["items"]] == [i["id"] for i in started["items"]]
    assert "answer" not in resumed.json()["items"][0]

    _take(client, stu, eid, "pre", "가", "SUM")
    assert client.get(f"/api/enrollments/{eid}/tests/pre/open", headers=stu).status_code == 404


def test_student_sees_materials_and_professor_grade(client: TestClient, fake_ai: FakeAI) -> None:
    prof = _auth(client, "prof@test.com", "professor")
    a = _setup_assignment(client, prof)
    aid = a["id"]
    up = client.post(
        f"/api/assignments/{aid}/materials",
        files={"file": ("join.txt", "조인은 두 테이블을 잇는다".encode(), "text/plain")},
        headers=prof,
    )
    assert up.status_code == 201, up.text

    stu = _auth(client, "stu@test.com", "student")
    other = _auth(client, "other@test.com", "student")
    en = _join(client, stu, a["join_code"])
    eid = en["id"]
    materials = en["assignment"]["materials"]
    assert [m["filename"] for m in materials] == ["join.txt"] and materials[0]["size"] > 0
    assert en["grade"] is None

    path = f"/api/enrollments/{eid}/materials/{materials[0]['id']}/file"
    got = client.get(path, headers=stu)
    assert got.status_code == 200 and "조인" in got.content.decode()
    # 참여하지 않은 학생은 받을 수 없다
    assert client.get(path, headers=other).status_code == 404

    r = client.put(f"/api/assignments/{aid}/students/{eid}/grade", json={"score": 88, "comment": "조인 설명이 정확하다"}, headers=prof)
    assert r.status_code == 200, r.text
    grade = client.get(f"/api/enrollments/{eid}", headers=stu).json()["grade"]
    assert grade["score"] == 88 and grade["comment"] == "조인 설명이 정확하다"


def test_production_requires_real_secret_key() -> None:
    for weak in (DEV_SECRET_KEY, "short-key", "여기에-긴-무작위-문자열-여기에-긴-무작위-문자열-여기에"):
        with pytest.raises(ValueError, match="SECRET_KEY"):
            Settings(_env_file=None, app_env="production", secret_key=weak)  # type: ignore[call-arg]
    strong = "k" * 48
    assert Settings(_env_file=None, app_env="production", secret_key=strong).secret_key == strong  # type: ignore[call-arg]
    # 개발 환경에서는 기본 키로도 돈다
    assert Settings(_env_file=None, app_env="development", secret_key=DEV_SECRET_KEY).app_env == "development"  # type: ignore[call-arg]


def test_answers_carry_grading_basis_by_source(client: TestClient, fake_ai: FakeAI) -> None:
    """학생이 받는 답안에는 채점 근거가 출처별로 들어가고, 틀린 부분은 답안에 실제로 있는 문장만 남는다."""
    prof = _auth(client, "prof@test.com", "professor")
    a = _setup_assignment(client, prof)
    comps = a["competencies"]
    r = client.put(
        f"/api/assignments/{a['id']}/items?phase=pre",
        json=[
            {"competency_id": comps[0]["id"], "type": "choice", "stem": "조인", "options": ["가", "나"], "answer": "가"},
            {"competency_id": comps[1]["id"], "type": "essay_short", "stem": "집계를 설명하라", "options": [], "answer": "모범 답안"},
        ],
        headers=prof,
    )
    assert r.status_code == 200, r.text
    stu = _auth(client, "stu@test.com", "student")
    eid = _join(client, stu, a["join_code"])["id"]

    fake_ai.grade_score = 40
    fake_ai.wrong_spans = ["조건은 WHERE 에 넣는다", "답안에 없는 문장"]
    essay = "합계를 구한다. 조건은 WHERE 에 넣는다"
    result = _take(client, stu, eid, "pre", "나", essay)

    choice, written = result["answers"]
    # 자동 채점은 교수가 정한 정답이 근거이고 틀린 답 전체를 표시한다
    assert choice["graded_by"] == "auto"
    assert choice["basis"] == [{"source": "criteria", "text": "교수가 정한 정답과 비교해 채점했다"}]
    assert choice["wrong_spans"] == ["나"]
    # AI 채점은 채점 기준, 학습 자료, AI 판단으로 나뉜다
    assert written["graded_by"] == "ai"
    assert [b["source"] for b in written["basis"]] == ["criteria", "material", "ai"]
    assert written["basis"][2]["text"] == "피드백"
    assert written["wrong_spans"] == ["조건은 WHERE 에 넣는다"]
    # 학생에게는 교수용 근거와 정답을 주지 않는다
    assert written["rationale"] == ""
    assert choice["expected_answer"] is None

    # 다시 불러와도 같다
    again = client.get(f"/api/enrollments/{eid}/tests/pre", headers=stu).json()
    assert again["answers"][1]["basis"] == written["basis"]

    def override(score: float, feedback: str = "") -> dict[str, Any]:
        r = client.patch(
            f"/api/assignments/{a['id']}/answers/{written['id']}", json={"score": score, "feedback": feedback}, headers=prof
        )
        assert r.status_code == 200, r.text
        return client.get(f"/api/enrollments/{eid}/tests/pre", headers=stu).json()["answers"][1]

    # 교수가 점수를 그대로 확인하면 AI 채점의 근거와 틀린 부분이 남는다
    kept = override(40)
    assert kept["graded_by"] == "professor"
    assert kept["basis"] == written["basis"]
    assert kept["wrong_spans"] == written["wrong_spans"]
    # 교수가 점수를 고치면 AI 가 남긴 근거와 틀린 부분 표시가 사라진다
    changed = override(100)
    assert changed["basis"] == [{"source": "criteria", "text": "교수가 답안을 확인하고 점수를 정했다"}]
    assert changed["wrong_spans"] == []
    # 교수가 쓴 피드백은 교수 채점 기준으로 나온다
    assert override(80, "집계 함수 설명이 정확하다")["basis"] == [{"source": "criteria", "text": "집계 함수 설명이 정확하다"}]
