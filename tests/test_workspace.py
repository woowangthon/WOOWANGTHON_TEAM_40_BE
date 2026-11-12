"""자리 4 작업공간: 자동 커밋, 붙여넣기, AI 수정안 수락과 거절, 실행 기록."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.models import Assignment, Enrollment, TaskEvent
from app.util import utcnow
from app.workspace import runner
from app.workspace.ai import WorkspaceReply
from app.workspace.diff import line_diff, replace_lines
from tests.conftest import FakeAI

CODE = "def add(a, b):\n    return a - b\n\nprint(add(1, 2))"
TEST = "from main import add\n\ndef test_add():\n    assert add(1, 2) == 3\n\ndef test_zero():\n    assert add(0, 0) == 0"


def _setup(client: TestClient) -> tuple[dict[str, str], int]:
    tokens = []
    for email, role in (("p@example.com", "professor"), ("s@example.com", "student")):
        r = client.post("/api/auth/signup", json={"email": email, "password": "password123", "name": role, "role": role})
        assert r.status_code == 201, r.text
        tokens.append((r.json()["token"], r.json()["user"]["id"]))
    with SessionLocal() as db:
        a = Assignment(
            professor_id=tokens[0][1], title="덧셈 함수", description="add 를 고친다", join_code="WS0001", status="open",
            deadline=utcnow() + timedelta(days=3), tool_slot=4, tool_name="workspace",
            tool_config={"files": [
                {"path": "main.py", "content": CODE},
                {"path": "test_main.py", "content": TEST, "readonly": True},
                {"path": "report.docx"},
            ]},
        )  # fmt: skip
        db.add(a)
        db.flush()
        en = Enrollment(assignment_id=a.id, student_id=tokens[1][1], stage="task")
        db.add(en)
        db.commit()
        eid = en.id
    return {"Authorization": f"Bearer {tokens[1][0]}"}, eid


def test_diff_helpers() -> None:
    diff, plus, minus = line_diff("a\nb\nc", "a\nB\nc\nd")
    assert (plus, minus) == (2, 1)
    assert {"type": "del", "no": 2, "text": "b"} in diff
    assert replace_lines("a\nb\nc", 2, 2, "x\ny") == "a\nx\ny\nc"
    assert replace_lines("a\nb", 2, 1, "new") == "a\nnew\nb"  # 삽입


def test_workspace_flow(client: TestClient, fake_ai: FakeAI, monkeypatch: pytest.MonkeyPatch) -> None:
    h, eid = _setup(client)
    base = f"/api/enrollments/{eid}/workspace"

    r = client.get(base, headers=h)
    assert r.status_code == 200, r.text
    ws = r.json()
    assert [f["path"] for f in ws["files"]] == ["main.py", "test_main.py", "report.docx"]
    assert ws["files"][0]["language"] == "python" and ws["files"][2]["kind"] == "doc"
    assert ws["commits"] == []

    # 내용이 같으면 커밋하지 않는다
    r = client.post(f"{base}/commit", json={"path": "main.py", "content": CODE, "trigger": "idle"}, headers=h)
    assert r.json()["committed"] is False
    # 읽기 전용 파일은 고칠 수 없다
    r = client.post(f"{base}/commit", json={"path": "test_main.py", "content": "x"}, headers=h)
    assert r.status_code == 403

    # 직접 수정 커밋
    typed = CODE.replace("print(add(1, 2))", "print(add(2, 3))")
    r = client.post(f"{base}/commit", json={"path": "main.py", "content": typed, "trigger": "focus_ai"}, headers=h)
    c = r.json()["commit"]
    assert c["author"] == "human" and c["trigger"] == "focus_ai" and (c["plus"], c["minus"]) == (1, 1)

    # 붙여넣기 커밋과 출처 기입
    pasted = typed + "\n# 외부에서 가져온 주석\n# 두 번째 줄\n# 세 번째 줄"
    r = client.post(
        f"{base}/commit",
        json={"path": "main.py", "content": pasted, "author": "paste", "trigger": "paste", "paste": {"chars": 40, "lines": 3}},
        headers=h,
    )
    pc = r.json()["commit"]
    assert pc["author"] == "paste" and pc["meta"]["paste"]["source"] is None
    r = client.post(f"{base}/commits/{pc['id']}/paste-source", json={"source": "수업 자료", "memo": "슬라이드 3"}, headers=h)
    assert r.json()["meta"]["paste"]["source"] == "수업 자료"

    # AI 수정안: 2번째 줄을 바꾼다
    def fake_parse(**kwargs: Any) -> Any:
        assert kwargs["output_model"] is WorkspaceReply
        assert "   2|     return a - b" in kwargs["system"]
        return WorkspaceReply(
            content="뺄셈을 덧셈으로 바꿨어요.", has_edit=True, edit_path="main.py",
            start_line=2, end_line=2, new_text="    return a + b", edit_reason="덧셈 함수가 빼고 있었다",
        )  # fmt: skip

    monkeypatch.setattr("app.workspace.ai.ai_client.parse", fake_parse)
    r = client.post(f"{base}/chat", json={"message": "테스트가 왜 실패해?", "path": "main.py", "selection": {"start_line": 1, "end_line": 2, "text": "def add"}}, headers=h)
    assert r.status_code == 200, r.text
    out = r.json()
    p = out["proposal"]
    assert p["old_text"] == "    return a - b" and p["status"] == "pending"
    assert out["messages"][1]["meta"]["proposal_id"] == p["id"]

    # 학생이 위에 한 줄을 더 넣은 뒤 수락해도 원문을 찾아 적용한다
    shifted = "# 메모\n" + pasted
    client.post(f"{base}/commit", json={"path": "main.py", "content": shifted}, headers=h)
    r = client.post(f"{base}/proposals/{p['id']}/accept", json={}, headers=h)
    assert r.status_code == 200, r.text
    acc = r.json()
    assert acc["proposal"]["status"] == "accepted"
    assert acc["commit"]["author"] == "ai" and acc["commit"]["meta"]["prompt"] == "테스트가 왜 실패해?"
    assert "    return a + b" in acc["file"]["content"].split("\n")[2]
    assert client.post(f"{base}/proposals/{p['id']}/accept", json={}, headers=h).status_code == 409

    # 두 번째 수정안은 학생이 고쳐서 반영한다. 작성자는 human, AI 원문은 따로 남는다
    monkeypatch.setattr(
        "app.workspace.ai.ai_client.parse",
        lambda **_: WorkspaceReply(content="주석", has_edit=True, edit_path="main.py", start_line=1, end_line=1, new_text="# AI 메모", edit_reason="주석 정리"),
    )
    p2 = client.post(f"{base}/chat", json={"message": "주석 고쳐줘"}, headers=h).json()["proposal"]
    r = client.post(f"{base}/proposals/{p2['id']}/accept", json={"text": "# 내가 고친 메모"}, headers=h)
    e2 = r.json()
    assert e2["proposal"]["status"] == "edited"
    assert e2["commit"]["author"] == "human" and e2["commit"]["meta"]["edited_from_ai"] is True
    assert e2["commit"]["meta"]["ai_text"] == "# AI 메모"
    # 세 번째 수정안은 거절. 거절은 커밋을 만들지 않는다
    monkeypatch.setattr(
        "app.workspace.ai.ai_client.parse",
        lambda **_: WorkspaceReply(content="설명만", has_edit=True, edit_path="main.py", start_line=1, end_line=0, new_text="import math", edit_reason="필요 없음"),
    )
    p3 = client.post(f"{base}/chat", json={"message": "import 추가"}, headers=h).json()["proposal"]
    before = len(client.get(base, headers=h).json()["commits"])
    r = client.post(f"{base}/proposals/{p3['id']}/reject", headers=h)
    assert r.json()["proposal"]["status"] == "rejected"
    assert len(client.get(base, headers=h).json()["commits"]) == before

    # 엉뚱한 줄 번호는 수정안으로 만들지 않는다
    monkeypatch.setattr(
        "app.workspace.ai.ai_client.parse",
        lambda **_: WorkspaceReply(content="x", has_edit=True, edit_path="main.py", start_line=99, end_line=120, new_text="y", edit_reason="r"),
    )
    assert client.post(f"{base}/chat", json={"message": "?"}, headers=h).json()["proposal"] is None

    # 실행은 Docker 대신 가짜 실행기로
    monkeypatch.setattr(
        runner, "run",
        lambda files, language, path, mode, stdin, tests: {"mode": mode, "language": language, "path": path, "compile_error": None, "tests": [{"name": "test_add", "passed": True}], "passed": 1, "total": 1, "stdout": "", "exit_code": 0, "duration": 0.1},
    )  # fmt: skip
    r = client.post(f"{base}/run", json={"path": "main.py", "mode": "test"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["commit"]["author"] == "run" and r.json()["commit"]["title"] == "테스트 실행 · 1/1 통과"
    assert client.get(base, headers=h).json()["last_run"]["passed"] == 1

    client.post(f"{base}/events", json=[{"kind": "focus_ai", "path": "main.py"}], headers=h)
    with SessionLocal() as db:
        kinds = {ev.kind for ev in db.query(TaskEvent).all()}
    assert {"workspace_init", "paste", "ai_request", "ai_accept", "ai_reject", "run", "focus_ai"} <= kinds

    # 기존 제출 흐름이 작업공간 커밋으로 그대로 돈다. 요약과 의심 신호는 공용 가짜 AI가 맡는다
    monkeypatch.setattr("app.workspace.ai.ai_client.parse", fake_ai.parse)
    r = client.post(f"/api/enrollments/{eid}/task/submit", headers=h)
    assert r.status_code == 200, r.text
    assert client.get(base, headers=h).json()["editable"] is False

    # 참여 정보를 지우면 수정안, 이벤트, 파일, 메시지가 외래 키 오류 없이 함께 지워진다
    with SessionLocal() as db:
        db.delete(db.get(Enrollment, eid))
        db.commit()


@pytest.mark.skipif(not runner.docker_available(), reason="Docker 가 꺼져 있다")
def test_runner_python_docker() -> None:
    files = {"main.py": CODE.replace("a - b", "a + b"), "test_main.py": TEST}
    r = runner.run(files, "python", "main.py", "run", "", [])
    assert r["stdout"].strip() == "3", r
    t = runner.run({"main.py": CODE, "test_main.py": TEST}, "python", "main.py", "test", "", [])
    assert (t["passed"], t["total"]) == (1, 2), t
    assert "assert add(1, 2) == 3" in t["tests"][0]["message"]


@pytest.mark.skipif(not runner.docker_available(), reason="Docker 가 꺼져 있다")
def test_runner_cpp_java_docker() -> None:
    cpp = '#include <iostream>\nint main(){int a,b;std::cin>>a>>b;std::cout<<a+b<<"\\n";}'
    r = runner.run({"main.cpp": cpp}, "cpp", "main.cpp", "test", "", [{"name": "합", "stdin": "1 2", "expected": "3"}])
    assert (r["passed"], r["total"]) == (1, 1), r
    assert runner.run({"main.cpp": "int main( {"}, "cpp", "main.cpp", "run", "", [])["compile_error"]
    java = 'public class Main { public static void main(String[] a){ System.out.println(new java.util.Scanner(System.in).nextLine()); } }'
    r = runner.run({"Main.java": java}, "java", "Main.java", "run", "자바", [])
    assert r["stdout"].strip() == "자바" and r["exit_code"] == 0, r
    assert runner.run({"loop.py": "while True: pass"}, "python", "loop.py", "run", "", [])["timed_out"]


def test_folders_rename_delete(client: TestClient) -> None:
    h, eid = _setup(client)
    base = f"/api/enrollments/{eid}/workspace"
    client.get(base, headers=h)
    assert client.post(f"{base}/files", json={"path": "utils/parse.py"}, headers=h).status_code == 201
    assert client.post(f"{base}/files", json={"path": "utils/more/deep.py"}, headers=h).status_code == 201
    # 파일과 폴더 이름이 겹치거나 경로가 이상하면 막는다
    for bad in ("utils", "main.py/x.py", "a//b.py", "../x.py", "utils/", " a.py"):
        assert client.post(f"{base}/files", json={"path": bad}, headers=h).status_code in (400, 409), bad
    client.post(f"{base}/commit", json={"path": "utils/parse.py", "content": "X = 1"}, headers=h)

    # 폴더 이름 바꾸기: 아래 파일이 모두 옮겨지고 각각 이력에 남는다
    r = client.post(f"{base}/files/rename", json={"path": "utils", "new_path": "lib", "folder": True}, headers=h)
    assert r.status_code == 200, r.text
    assert sorted(f["path"] for f in r.json()["files"]) == ["lib/more/deep.py", "lib/parse.py"]
    assert all(c["trigger"] == "rename" for c in r.json()["commits"])
    paths = [f["path"] for f in client.get(base, headers=h).json()["files"]]
    assert "lib/parse.py" in paths and not any(p.startswith("utils/") for p in paths)
    # 교수 기본 파일은 옮기거나 지울 수 없다
    assert client.post(f"{base}/files/rename", json={"path": "main.py", "new_path": "x.py"}, headers=h).status_code == 403
    assert client.post(f"{base}/files/delete", json={"path": "main.py"}, headers=h).status_code == 403
    # 코드와 보고서 사이 확장자 변경은 막는다
    assert client.post(f"{base}/files/rename", json={"path": "lib/parse.py", "new_path": "lib/parse.docx"}, headers=h).status_code == 400

    # 폴더 삭제: 지운 내용은 이력에 남는다
    r = client.post(f"{base}/files/delete", json={"path": "lib", "folder": True}, headers=h)
    assert r.status_code == 200, r.text
    deleted = {c["file"]: c for c in r.json()["commits"]}
    assert deleted["lib/parse.py"]["meta"]["deleted_text"] == "X = 1"
    assert deleted["lib/parse.py"]["diff"] == [{"type": "del", "no": 1, "text": "X = 1"}]
    assert [f["path"] for f in client.get(base, headers=h).json()["files"]] == ["main.py", "test_main.py", "report.docx"]


@pytest.mark.skipif(not runner.docker_available(), reason="Docker 가 꺼져 있다")
def test_runner_nested_folders_docker() -> None:
    java_main = "package com.demo;\nimport com.demo.util.Greeter;\npublic class Main { public static void main(String[] a){ System.out.println(Greeter.hi()); } }"
    java_util = "package com.demo.util;\npublic class Greeter { public static String hi(){ return \"hi from package\"; } }"
    r = runner.run({"src/com/demo/Main.java": java_main, "src/com/demo/util/Greeter.java": java_util}, "java", "src/com/demo/Main.java", "run", "", [])
    assert r["stdout"].strip() == "hi from package", r
    files = {"app/main.py": "from lib.calc import add\nprint(add(2, 3))", "lib/calc.py": "def add(a, b):\n    return a + b", "tests/test_calc.py": "from lib.calc import add\n\ndef test_add():\n    assert add(1, 1) == 2"}
    assert runner.run(files, "python", "app/main.py", "run", "", [])["stdout"].strip() == "5"
    t = runner.run(files, "python", "tests/test_calc.py", "test", "", [])
    assert (t["passed"], t["total"]) == (1, 1), t
    cpp = {"src/main.cpp": '#include "util.h"\n#include <iostream>\nint main(){ std::cout << twice(21) << "\\n"; }', "include/util.h": "inline int twice(int x){ return x * 2; }"}
    assert runner.run(cpp, "cpp", "src/main.cpp", "run", "", [])["stdout"].strip() == "42"
