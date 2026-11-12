"""자리 4 작업공간 시연 데이터를 DB에 바로 만든다. 학생은 과제 단계에서 시작한다.

    DEMO_PASSWORD=비밀번호 .venv/bin/python scripts/seed_workspace_demo.py [--reset]

사전평가와 학습 단계는 건너뛴다. 과제 단계 화면만 보여 주는 시연용이다.
--reset 을 주면 이 스크립트가 만든 과제를 지우고 처음 상태로 다시 만든다.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import Assignment, Competency, CompetencyState, Enrollment, User  # noqa: E402
from app.security import hash_password  # noqa: E402
from app.util import utcnow  # noqa: E402

PROFESSOR = ("ws.professor@example.com", "시연 교수")
STUDENT = ("ws.student@example.com", "시연 학생")
JOIN_CODE = "WSDEMO"

SCHEDULER = '''from collections import deque


def round_robin(tasks, quantum):
    """Round-robin 방식으로 작업을 스케줄링합니다.

    tasks: (이름, 실행 시간) 목록
    반환값: (이름, 이번에 실행한 시간) 목록
    """
    queue = deque(tasks)
    timeline = []

    while queue:
        name, remaining = queue.popleft()
        run_time = min(quantum, remaining)
        timeline.append((name, run_time))
        remaining -= run_time

        if remaining > 0:
            queue.append((name, remaining))

    return timeline


if __name__ == "__main__":
    tasks = [("P1", 5), ("P2", 3), ("P3", 7)]
    print(round_robin(tasks, quantum=2))
'''

TESTS = '''from scheduler import round_robin


def test_basic_order():
    assert round_robin([("P1", 4), ("P2", 2)], 2)[:2] == [("P1", 2), ("P2", 2)]


def test_shorter_than_quantum():
    assert round_robin([("P1", 1)], 3) == [("P1", 1)]


def test_empty():
    assert round_robin([], 2) == []


def test_single_task():
    assert round_robin([("P1", 3)], 2) == [("P1", 2), ("P1", 1)]


def test_zero_burst():
    tasks = [("P1", 2), ("P0", 0), ("P2", 2)]
    assert round_robin(tasks, 2) == [("P1", 2), ("P2", 2)]
'''

REPORT_OUTLINE = """# 1. 알고리즘 개요

# 2. 구현 방법

# 3. quantum 크기별 비교
"""

DESCRIPTION = """Round Robin CPU 스케줄러를 구현하고 시간 할당량(quantum)이 결과에 주는 영향을 분석한다.

1. scheduler.py 의 round_robin 함수를 완성한다. test_scheduler.py 의 테스트 5개를 모두 통과해야 한다.
2. report.docx 에 알고리즘 개요, 구현 방법, quantum 크기별 비교를 쓴다.

AI 사용은 허용한다. AI 에게 무엇을 물었고 어떤 수정안을 받아들였는지는 모두 기록되어 교수에게 보인다."""


def user(db, email: str, name: str, role: str, password: str) -> User:  # type: ignore[no-untyped-def]
    u = db.query(User).filter(User.email == email).first()
    if u is None:
        u = User(email=email, name=name, role=role, password_hash=hash_password(password))
        db.add(u)
        db.flush()
    return u


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    password = os.environ.get("DEMO_PASSWORD", "")
    if len(password) < 8:
        print("환경 변수 DEMO_PASSWORD 에 8자 이상 비밀번호를 넣어야 한다")
        return 1
    init_db()
    with SessionLocal() as db:
        prof = user(db, *PROFESSOR, "professor", password)
        stu = user(db, *STUDENT, "student", password)
        old = db.query(Assignment).filter(Assignment.join_code == JOIN_CODE).first()
        if old is not None:
            if not args.reset:
                en = db.query(Enrollment).filter(Enrollment.assignment_id == old.id).first()
                print(f"이미 있다. 과제 id {old.id}, 참여 id {en.id if en else '-'}. 처음부터 다시 만들려면 --reset")
                return 0
            for en in db.query(Enrollment).filter(Enrollment.assignment_id == old.id).all():
                db.delete(en)
            db.flush()
            db.delete(old)
            db.flush()

        a = Assignment(
            professor_id=prof.id,
            title="Round Robin 스케줄러 구현과 분석",
            subject="운영체제",
            description=DESCRIPTION,
            join_code=JOIN_CODE,
            status="open",
            deadline=utcnow() + timedelta(days=14),
            task_kind="performance",
            tool_slot=4,
            tool_name="workspace",
            tool_config={
                "files": [
                    {"path": "scheduler.py", "content": SCHEDULER},
                    {"path": "test_scheduler.py", "content": TESTS, "readonly": True},
                    {"path": "report.docx", "content": REPORT_OUTLINE},
                ]
            },
        )
        db.add(a)
        db.flush()
        comp = Competency(assignment_id=a.id, order=0, name="CPU 스케줄링", description="Round Robin 과 시간 할당량", pass_score=60, grading_criteria="시간 할당량의 영향을 설명하면 만점")
        db.add(comp)
        db.flush()
        en = Enrollment(assignment_id=a.id, student_id=stu.id, stage="task")
        db.add(en)
        db.flush()
        db.add(CompetencyState(enrollment_id=en.id, competency_id=comp.id, pre_score=55, current_score=80))
        db.commit()
        print(f"과제 id {a.id}, 참여 id {en.id}")
        print(f"학생 로그인: {STUDENT[0]} / DEMO_PASSWORD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
