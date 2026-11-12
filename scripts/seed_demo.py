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

# 작업공간 시연 과제의 시작 파일과 설명은 seed_workspace_demo.py 의 것을 같이 쓴다
from seed_workspace_demo import DESCRIPTION, REPORT_OUTLINE, SCHEDULER, TESTS  # noqa: E402


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

    a = make_assignment(
        base,
        prof,
        {
            "title": "Round Robin 스케줄러 구현과 분석",
            "subject": "운영체제",
            "description": DESCRIPTION,
            "deadline": deadline,
            "task_kind": "performance",
            "tool_slot": 4,
            "tool_name": "workspace",
            "tool_config": {
                "files": [
                    {"path": "scheduler.py", "content": SCHEDULER},
                    {"path": "test_scheduler.py", "content": TESTS, "readonly": True},
                    {"path": "report.docx", "content": REPORT_OUTLINE},
                ],
                # 교수 지정 실행 환경. 학생이 처음 고를 때 서버가 이미지를 만든다
                "environment": {
                    "preset": "ubuntu-22.04",
                    "name": "운영체제 과제 채점 환경",
                    "required": False,
                    "apt": [],
                    "pip": [],
                    "cflags": "-std=c++17 -O2 -Wall",
                },
            },
        },
        [
            {"name": "CPU 스케줄링 개념", "description": "선점과 비선점, 대기 시간과 반환 시간", "pass_score": 60, "grading_criteria": "선점형과 비선점형의 차이를 구분하고 대기 시간과 반환 시간을 정확히 정의하면 만점이다. 용어만 나열하면 40점 이하다."},
            {"name": "Round Robin 동작", "description": "준비 큐와 시간 할당량으로 실행 순서를 정한다", "pass_score": 60, "grading_criteria": "시간 할당량이 끝난 작업이 준비 큐의 뒤로 가는 과정을 순서대로 설명하면 만점이다. 순서가 틀리면 30점 이하다."},
            {"name": "시간 할당량 분석", "description": "할당량 크기가 응답 시간과 문맥 교환에 주는 영향", "pass_score": 60, "grading_criteria": "할당량이 작을 때와 클 때의 장단점을 문맥 교환 비용과 응답 시간으로 설명하면 만점이다. 한쪽만 쓰면 50점 이하다."},
        ],
        [
            {"type": "choice", "stem": "선점형 스케줄링에 해당하는 것은", "options": ["FCFS", "비선점 SJF", "Round Robin", "비선점 우선순위"], "answer": "Round Robin"},
            {"type": "short", "stem": "Round Robin 에서 한 작업이 한 번에 CPU 를 쓸 수 있는 최대 시간을 무엇이라 하는가", "options": [], "answer": "시간 할당량|타임 퀀텀|time quantum|quantum"},
            {"type": "essay_short", "stem": "시간 할당량을 아주 작게 잡으면 어떤 문제가 생기는지 설명하라", "options": [], "answer": "문맥 교환이 잦아져 실제 작업에 쓰는 시간보다 교환 비용이 커진다"},
        ],
        [
            {"type": "choice", "stem": "실행 중인 작업을 운영체제가 강제로 멈출 수 있는 방식은", "options": ["비선점형", "선점형", "일괄 처리", "단일 작업"], "answer": "선점형"},
            {"type": "short", "stem": "Round Robin 에서 준비 상태의 작업이 차례를 기다리는 자료구조는 무엇인가", "options": [], "answer": "큐|준비 큐|ready queue|queue"},
            {"type": "essay_short", "stem": "시간 할당량을 아주 크게 잡으면 Round Robin 이 어떤 방식과 같아지는지 이유와 함께 설명하라", "options": [], "answer": "모든 작업이 할당량 안에 끝나므로 먼저 온 순서대로 처리하는 FCFS 와 같아진다"},
        ],
        [
            {"target": "ai", "text": "학습 단계에서는 round_robin 함수의 완성된 코드를 보여 주지 않는다"},
            {"target": "student", "text": "과제 단계에서 AI 를 쓴 내용은 모두 기록되어 교수에게 보인다"},
        ],
    )
    print(f"작업공간 과제 생성: id {a['id']} 참여 코드 {a['join_code']}")
    print(f"심사용 계정: 교수 {PROFESSOR['email']} / 학생 {STUDENT['email']} (비밀번호는 DEMO_PASSWORD)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
