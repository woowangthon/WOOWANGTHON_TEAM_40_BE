"""작업공간 AI 패널. 과제 단계지만 AI 사용을 막지 않는다. 대신 모든 요청과 수정안, 수락 여부를 기록한다.

수정안은 구조화 출력의 edit 필드로 받는다(Messages API 의 edit_file 도구와 같은 역할). 공급자에 상관없이
ai_client.parse 하나로 돈다. AI 는 파일을 직접 고치지 않고 제안만 하며, 적용은 학생이 수락할 때 서버가 한다.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..ai.client import ai_client
from ..ai.prompts import _assignment_section, _rules_section
from ..ai.services import _merge_consecutive
from ..config import settings
from ..models import ArtifactVersion, Enrollment, TaskAttempt, WorkspaceFile


class WorkspaceReply(BaseModel):
    content: str = Field(description="학생에게 보여 줄 답. 수정안이 있으면 무엇을 왜 바꾸는지 짧게 설명한다. 마크다운 코드 블록을 쓸 수 있다.")
    has_edit: bool = Field(description="파일 수정안을 낼 때만 true")
    edit_path: str = Field(description="고칠 파일 경로. 수정안이 없으면 빈 문자열")
    start_line: int = Field(description="바꿀 첫 줄 번호, 1부터. 수정안이 없으면 0")
    end_line: int = Field(description="바꿀 마지막 줄 번호, 포함. 줄을 지우지 않고 start_line 앞에 넣기만 하면 start_line - 1")
    new_text: str = Field(description="start_line..end_line 을 대신할 새 내용 전체. 줄 번호 없이, 들여쓰기를 포함해 그대로 쓴다")
    edit_reason: str = Field(description="수정 이유 한 문장. 수정안이 없으면 빈 문자열")


ROLE = """너는 대학 프로그래밍 과제 작업공간에 붙은 AI 도우미다. 이 과제 단계에서는 AI 사용이 허용되어 있고,
학생이 AI 를 어떻게 썼는지는 서비스가 기록해 교수에게 보여 준다. 그러니 학생의 요청을 숨김없이 도와준다.
모든 응답은 한국어로 쓴다. 짧고 정확하게 답한다.

## 수정안 규칙
1. 학생이 코드를 고치거나 쓰거나 추가해 달라고 하면 has_edit 를 true 로 두고 수정안을 하나만 낸다.
   설명이나 질문만 원하면 has_edit 는 false 다.
2. 수정안은 아래 파일 내용의 줄 번호를 기준으로 한다. start_line..end_line 을 new_text 로 통째로 바꾼다.
   줄을 지우지 않고 넣기만 하려면 end_line = start_line - 1 로 둔다. 파일 끝에 붙이려면 start_line = 마지막 줄 번호 + 1.
3. new_text 에는 바뀌는 범위의 최종 내용 전체를 줄 번호 없이 쓴다. 원래 들여쓰기를 지킨다. 바꾸지 않는 줄은 범위에 넣지 않는다.
4. 읽기 전용 파일은 고치지 않는다.
5. content 에는 무엇을 왜 바꾸는지 두세 문장으로 쓴다. new_text 를 content 에 다시 붙이지 않는다.
"""


def _numbered(content: str, limit: int) -> str:
    lines = content.split("\n")
    body = "\n".join(f"{i + 1:>4}| {line}" for i, line in enumerate(lines))
    return body if len(body) <= limit else body[:limit] + "\n…(이하 생략)"


def _files_section(files: list[WorkspaceFile], focus: str) -> str:
    budget = settings.ai_max_context_chars
    ordered = sorted(files, key=lambda f: (f.path != focus, f.path))
    parts = []
    for f in ordered:
        if f.kind != "code":
            text = f.content if len(f.content) <= 6000 else f.content[:6000] + "\n…(이하 생략)"
            parts.append(f"### {f.path} (보고서 docx 의 본문 텍스트. 이 패널에서는 수정안을 내지 않고 설명과 제안 문장만 쓴다)\n{text or '(비어 있음)'}")
            continue
        share = max(2000, budget // max(1, len(files)))
        tag = " (읽기 전용)" if f.readonly else ""
        parts.append(f"### {f.path}{tag} [{f.language}]\n```\n{_numbered(f.content, share)}\n```")
    return "## 작업공간 파일\n" + "\n\n".join(parts)


def _last_run_section(attempt: TaskAttempt) -> str:
    runs = [v for v in attempt.versions if v.source == "workspace" and (v.meta or {}).get("author") == "run"]
    if not runs:
        return ""
    r: dict[str, Any] = runs[-1].meta.get("run", {})
    lines = [f"## 마지막 실행 ({r.get('mode')}, {r.get('path')})"]
    if r.get("compile_error"):
        lines.append(f"컴파일 오류:\n{str(r['compile_error'])[:1500]}")
    if r.get("total"):
        lines.append(f"테스트 {r.get('passed')}/{r.get('total')} 통과")
        for t in r.get("tests", []):
            if not t.get("passed"):
                lines.append(f"- 실패 {t.get('name')}: {str(t.get('message', ''))[:300]}")
    if r.get("stdout"):
        lines.append(f"출력:\n{str(r['stdout'])[:1500]}")
    return "\n".join(lines)


def reply(
    db: Session,
    enrollment: Enrollment,
    attempt: TaskAttempt,
    files: list[WorkspaceFile],
    message: str,
    focus_path: str,
    selection: dict[str, Any] | None,
) -> WorkspaceReply:
    a = enrollment.assignment
    system = "\n\n".join(
        s
        for s in [
            ROLE,
            _rules_section(a),
            _assignment_section(a),
            _files_section(files, focus_path),
            _last_run_section(attempt),
        ]
        if s
    )
    history = [m for m in enrollment.messages if m.stage == "task" and m.attempt_no == attempt.attempt_no][-20:]
    messages: list[dict[str, Any]] = [
        {"role": "user" if m.role == "student" else "assistant", "content": m.content} for m in history
    ]
    user = message
    if selection and selection.get("text"):
        user = (
            f"[선택한 범위: {focus_path} {selection.get('start_line')}–{selection.get('end_line')}줄]\n"
            f"```\n{str(selection['text'])[:4000]}\n```\n\n{message}"
        )
    elif focus_path:
        user = f"[지금 보고 있는 파일: {focus_path}]\n{message}"
    messages.append({"role": "user", "content": user})
    return ai_client.parse(
        purpose="chat_workspace",
        system=system,
        messages=_merge_consecutive(messages),
        output_model=WorkspaceReply,
        effort=settings.ai_effort_chat,
        max_tokens=6000,
        db=db,
    )


def run_versions(attempt: TaskAttempt) -> list[ArtifactVersion]:
    return [v for v in attempt.versions if v.source == "workspace" and (v.meta or {}).get("author") == "run"]
