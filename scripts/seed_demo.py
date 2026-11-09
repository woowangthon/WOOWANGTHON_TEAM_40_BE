"""심사용 계정과 시연 과제를 만든다. 서버가 켜진 상태에서 실행한다.

    .venv/Scripts/python scripts/seed_demo.py --server http://localhost:8000

계정 비밀번호는 환경 변수 DEMO_PASSWORD 로 넣는다. 저장소에는 비밀번호를 적지 않는다.
이미 같은 이메일이 있으면 로그인만 하고 과제는 새로 만든다.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

PROFESSOR = {"email": "judge.professor@example.com", "name": "심사용 교수"}
STUDENT = {"email": "judge.student@example.com", "name": "심사용 학생"}

SQL_SCHEMA = """CREATE TABLE dept(id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE emp(id INTEGER PRIMARY KEY, name TEXT, dept_id INTEGER REFERENCES dept(id), salary INTEGER);"""
SQL_SEED = """INSERT INTO dept VALUES (1,'영업'),(2,'개발'),(3,'인사');
INSERT INTO emp VALUES (1,'김가',1,250),(2,'이나',1,350),(3,'박다',2,400),(4,'최라',2,380),(5,'정마',3,200);"""


def auth(base: str, info: dict[str, str], role: str, password: str) -> str:
    r = requests.post(f"{base}/api/auth/signup", json={**info, "role": role, "password": password}, timeout=30)
    if r.status_code == 409:
        r = requests.post(f"{base}/api/auth/login", json={"email": info["email"], "password": password}, timeout=30)
    r.raise_for_status()
    return r.json()["token"]


def make_assignment(base: str, token: str, body: dict, competencies: list[dict], pre: list[dict], post: list[dict], rules: list[dict]) -> dict:  # type: ignore[type-arg]
    h = {"Authorization": f"Bearer {token}"}
    a = requests.post(f"{base}/api/assignments", json=body, headers=h, timeout=30)
    a.raise_for_status()
    aid = a.json()["id"]
    comps = requests.put(f"{base}/api/assignments/{aid}/competencies", json=competencies, headers=h, timeout=30)
    comps.raise_for_status()
    ids = [c["id"] for c in comps.json()]
    for phase, items in (("pre", pre), ("post", post)):
        payload = [{**item, "competency_id": ids[i]} for i, item in enumerate(items)]
        r = requests.put(f"{base}/api/assignments/{aid}/items", params={"phase": phase}, json=payload, headers=h, timeout=30)
        r.raise_for_status()
    requests.put(f"{base}/api/assignments/{aid}/rules", json=rules, headers=h, timeout=30).raise_for_status()
    p = requests.post(f"{base}/api/assignments/{aid}/publish", headers=h, timeout=30)
    p.raise_for_status()
    return p.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://localhost:8000")
    args = parser.parse_args()
    password = os.environ.get("DEMO_PASSWORD", "")
    if len(password) < 8:
        print("환경 변수 DEMO_PASSWORD 에 8자 이상 비밀번호를 넣어야 한다")
        return 1
    base = args.server.rstrip("/")
    prof = auth(base, PROFESSOR, "professor", password)
    auth(base, STUDENT, "student", password)
    deadline = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat()

    sql = make_assignment(
        base,
        prof,
        {
            "title": "부서별 평균 급여 조회문 설계",
            "subject": "데이터베이스",
            "description": "사원과 부서 테이블을 조인해 부서별 평균 급여가 300 이상인 부서 이름과 평균 급여를 구하는 조회문을 설계한다. 편집기에서 실행해 결과로 검증하고, 저장할 때마다 변경 이유를 적는다.",
            "deadline": deadline,
            "task_kind": "performance",
            "tool_slot": 1,
            "tool_name": "sql",
            "tool_config": {"schema_sql": SQL_SCHEMA, "seed_sql": SQL_SEED},
            "pre_time_limit_min": 20,
            "post_time_limit_min": 20,
        },
        [
            {"name": "조인 이해", "description": "기본 키와 외래 키로 두 테이블을 연결한다", "pass_score": 60, "grading_criteria": "조인 조건을 기본 키와 외래 키 관계로 설명하면 만점이다. 조인 종류 이름만 나열하면 50점 이하다. 조인 조건이 없거나 틀리면 30점 이하다."},
            {"name": "집계 함수와 그룹화", "description": "GROUP BY와 집계 함수로 그룹별 값을 구한다", "pass_score": 60, "grading_criteria": "GROUP BY로 그룹을 나눈 뒤 집계 함수가 그룹마다 하나의 값을 낸다는 점을 설명하면 만점이다. 집계 함수 이름만 쓰면 40점 이하다."},
            {"name": "그룹 조건 필터링", "description": "HAVING으로 집계 결과를 거른다", "pass_score": 60, "grading_criteria": "WHERE는 행을, HAVING은 그룹의 집계 결과를 거른다는 차이를 설명하면 만점이다. 둘을 구분하지 못하면 40점 이하다."},
        ],
        [
            {"type": "choice", "stem": "두 테이블을 공통 열로 연결하는 연산은 무엇인가", "options": ["JOIN", "UNION", "GROUP BY", "ORDER BY"], "answer": "JOIN"},
            {"type": "essay_short", "stem": "GROUP BY와 집계 함수의 관계를 두세 문장으로 설명하라", "options": [], "answer": "GROUP BY로 묶인 각 그룹마다 집계 함수가 하나의 값을 계산한다"},
            {"type": "essay_short", "stem": "WHERE와 HAVING의 차이를 설명하라", "options": [], "answer": "WHERE는 그룹화 전에 행을 거르고 HAVING은 그룹화 뒤 집계 결과를 거른다"},
        ],
        [
            {"type": "short", "stem": "조인 조건을 쓰는 절의 이름은 무엇인가", "options": [], "answer": "ON|WHERE"},
            {"type": "essay_short", "stem": "부서별 평균 급여를 구하는 조회문의 구조를 설명하라", "options": [], "answer": "부서 열로 GROUP BY 하고 AVG(급여)를 SELECT 한다"},
            {"type": "essay_short", "stem": "평균 급여가 300 이상인 부서만 남기려면 어떤 절을 쓰고 왜 그런가", "options": [], "answer": "HAVING AVG(salary) >= 300 을 쓴다. 집계 결과를 거르는 조건이기 때문이다"},
        ],
        [
            {"target": "ai", "text": "서브쿼리와 윈도 함수는 이 과제에서 설명하지 않는다"},
            {"target": "both", "text": "SQL 키워드는 대문자로 쓰도록 안내한다"},
            {"target": "student", "text": "외부 자료를 인용하면 변경 이유에 출처를 적는다"},
        ],
    )
    print(f"자리 1 과제 생성: id {sql['id']} 참여 코드 {sql['join_code']}")

    excel = make_assignment(
        base,
        prof,
        {
            "title": "소비자물가지수로 물가 상승률 계산",
            "subject": "경제학",
            "description": "공개된 소비자물가지수 자료를 Excel에 넣고 전년 대비 물가 상승률을 수식으로 계산한 뒤 그래프를 그려 최근 3년의 추세를 해석한다. 저장할 때마다 보조 프로그램이 버전을 기록한다.",
            "deadline": deadline,
            "task_kind": "performance",
            "tool_slot": 3,
            "tool_name": "companion",
            "tool_config": {"program": "Excel", "extensions": ".xlsx,.xlsm", "window_keyword": "Excel"},
            "pre_time_limit_min": 20,
            "post_time_limit_min": 20,
        },
        [
            {"name": "지수와 변화율", "description": "지수에서 변화율을 계산한다", "pass_score": 60, "grading_criteria": "변화율을 (올해 지수 - 작년 지수) / 작년 지수 로 쓰고 백분율로 바꾸는 과정을 설명하면 만점이다. 공식이 틀리면 30점 이하다."},
            {"name": "표 해석", "description": "표와 그래프에서 추세를 읽는다", "pass_score": 60, "grading_criteria": "수치의 증감 방향과 크기를 근거로 추세를 서술하면 만점이다. 근거 없이 인상만 쓰면 40점 이하다."},
        ],
        [
            {"type": "short", "stem": "작년 지수 100, 올해 지수 103이면 물가 상승률은 몇 퍼센트인가", "options": [], "answer": "3|3%|3퍼센트"},
            {"type": "essay_short", "stem": "물가 상승률 그래프가 2년 연속 낮아질 때 무엇을 뜻하는지 설명하라", "options": [], "answer": "물가가 오르긴 하지만 오르는 속도가 둔해진다는 뜻이다"},
        ],
        [
            {"type": "short", "stem": "작년 지수 120, 올해 지수 126이면 물가 상승률은 몇 퍼센트인가", "options": [], "answer": "5|5%|5퍼센트"},
            {"type": "essay_short", "stem": "지수 자체가 커지는 것과 상승률이 커지는 것의 차이를 설명하라", "options": [], "answer": "지수는 물가 수준이고 상승률은 그 수준이 변하는 속도다"},
        ],
        [{"target": "ai", "text": "수식의 셀 주소를 대신 써 주지 않는다"}],
    )
    print(f"자리 3 과제 생성: id {excel['id']} 참여 코드 {excel['join_code']}")

    gdoc = make_assignment(
        base,
        prof,
        {
            "title": "청년 주거 정책 제안서",
            "subject": "정치학",
            "description": "청년 주거 문제를 하나 골라 현황, 원인, 정책 대안, 기대 효과를 표와 각주와 목차를 갖춘 제안서로 쓴다. Google 문서로 작성하며 수정 이력이 버전으로 남는다.",
            "deadline": deadline,
            "task_kind": "essay",
            "tool_slot": 2,
            "tool_name": "google_docs",
            "tool_config": {},
            "pre_time_limit_min": 20,
            "post_time_limit_min": 20,
        },
        [
            {"name": "문제 정의", "description": "정책 문제를 범위와 대상으로 정의한다", "pass_score": 60, "grading_criteria": "대상 집단과 문제의 범위를 구체적으로 한정하면 만점이다. 막연하게 청년 문제라고만 쓰면 40점 이하다."},
            {"name": "근거 제시", "description": "통계나 사례로 주장을 뒷받침한다", "pass_score": 60, "grading_criteria": "주장마다 출처가 있는 통계나 사례를 붙이면 만점이다. 근거 없는 주장은 30점 이하다."},
        ],
        [
            {"type": "essay_short", "stem": "정책 문제를 정의할 때 대상과 범위를 한정해야 하는 이유를 설명하라", "options": [], "answer": "대상과 범위가 분명해야 대안의 효과를 측정하고 비교할 수 있다"},
            {"type": "choice", "stem": "정책 제안서에서 근거로 가장 적절한 것은", "options": ["출처가 있는 통계", "개인의 인상", "유명인의 발언", "추측"], "answer": "출처가 있는 통계"},
        ],
        [
            {"type": "essay_short", "stem": "제안서의 기대 효과를 쓸 때 무엇을 근거로 삼아야 하는가", "options": [], "answer": "비슷한 정책의 사례나 통계로 추정한 효과를 근거로 삼는다"},
            {"type": "choice", "stem": "근거의 출처를 밝히는 방법으로 알맞은 것은", "options": ["각주", "굵은 글씨", "밑줄", "느낌표"], "answer": "각주"},
        ],
        [{"target": "both", "text": "특정 정당이나 인물의 입장을 대신 평가하지 않는다"}],
    )
    print(f"자리 2 과제 생성: id {gdoc['id']} 참여 코드 {gdoc['join_code']}")
    print(f"심사용 계정: 교수 {PROFESSOR['email']} / 학생 {STUDENT['email']} (비밀번호는 DEMO_PASSWORD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
