"""작업공간 AI 패널. 과제 단계지만 AI 사용을 막지 않는다. 대신 모든 요청과 수정안, 수락 여부를 기록한다.

수정안은 구조화 출력의 edit 필드로 받는다(Messages API 의 edit_file 도구와 같은 역할). 공급자에 상관없이
ai_client.parse 하나로 돈다. AI 는 파일을 직접 고치지 않고 제안만 하며, 적용은 학생이 수락할 때 서버가 한다.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..ai.client import ai_client
from ..ai.prompts import _assignment_section, _rules_section
from ..ai.services import _merge_consecutive
from ..config import settings
from ..ai.extract import extract_text as extract_file_text
from ..ai.retrieval import build_context
from ..models import ArtifactVersion, ChatAttachment, Enrollment, TaskAttempt, WorkspaceFile
from .docx_store import doc_path, heading_levels, locked_lines, table_lines


class WorkspaceReply(BaseModel):
    content: str = Field(description="학생에게 보여 줄 답. 수정안이 있으면 무엇을 왜 바꾸는지 짧게 설명한다. 설명이나 정리를 원하면 자세히 쓴다. 소제목, 목록, 굵게, 코드 블록을 쓸 수 있다.")
    has_edit: bool = Field(description="파일 수정안을 낼 때만 true")
    edit_path: str = Field(description="고칠 파일 경로. 수정안이 없으면 빈 문자열")
    start_line: int = Field(description="바꿀 첫 줄 번호, 1부터. 수정안이 없으면 0")
    end_line: int = Field(description="바꿀 마지막 줄 번호, 포함. 줄을 지우지 않고 start_line 앞에 넣기만 하면 start_line - 1")
    new_text: str = Field(description="start_line..end_line 을 대신할 새 내용 전체. 줄 번호 없이, 들여쓰기를 포함해 그대로 쓴다")
    edit_reason: str = Field(description="수정 이유 한 문장. 수정안이 없으면 빈 문자열")


ROLE = """너는 대학 프로그래밍 과제 작업공간(코드와 보고서)에 붙은 AI 도우미다. 이 과제 단계에서는 AI 사용이 허용되어 있고,
학생이 AI 를 어떻게 썼는지는 서비스가 기록해 교수에게 보여 준다. 그러니 학생의 요청을 숨김없이 도와준다.
모든 응답은 한국어로 쓴다. 단순한 질문에는 짧고 정확하게 답한다.

## 말뜻
- '과제', '과제 파일', '과제 내용', '과제 자료' 는 교수가 낸 것을 말한다. 과제 설명, 교수가 올린 과제 문제 파일,
  교수가 준 시작 파일(코드, 테스트, 보고서 틀), 학습 자료, 교수가 정한 역량과 마감이다. 학생이 만든 파일이 아니다.
- 과제를 정리하거나 요약해 달라고 하면 파일 목록을 말하지 말고 내용을 항목별로 자세히 정리한다.
  과제 목표, 구현해야 할 것(함수 이름, 입력과 출력, 지켜야 할 조건), 시작 코드에 이미 있는 것과 빈 곳,
  테스트가 확인하는 것(테스트마다 무엇을 넣고 무엇을 기대하는지), 보고서에 써야 할 절, 평가 역량, 마감을 다룬다.
  주어진 자료에 없는 내용은 지어내지 않는다. 이런 정리와 설명은 길어져도 된다.
- 답에는 짧은 소제목(### 제목), 목록(- 항목), 굵게(**강조**), 코드(`이름`)를 쓸 수 있다. 표와 그 밖의 마크다운은 쓰지 않는다.
  소제목과 목록 항목은 반드시 각각 새 줄에서 시작한다(content 문자열 안에 줄바꿈 \\n 을 넣는다). 한 줄에 이어 쓰지 않는다.

## 수정안 규칙
1. 학생이 코드나 보고서를 고치거나 쓰거나 추가해 달라고 하면 has_edit 를 true 로 두고 수정안을 하나만 낸다.
   설명이나 질문만 원하면 has_edit 는 false 다.
2. 수정안은 아래 파일 내용의 줄 번호를 기준으로 한다. start_line..end_line 을 new_text 로 통째로 바꾼다.
   줄을 지우지 않고 넣기만 하려면 end_line = start_line - 1 로 둔다. 파일 끝에 붙이려면 start_line = 마지막 줄 번호 + 1.
3. new_text 에는 바뀌는 범위의 최종 내용 전체를 줄 번호 없이 쓴다. 원래 들여쓰기를 지킨다. 바꾸지 않는 줄은 범위에 넣지 않는다.
4. 읽기 전용 파일은 고치지 않는다.
5. content 에는 무엇을 왜 바꾸는지 두세 문장으로 쓴다. new_text 를 content 에 다시 붙이지 않는다.
   '학습 자료 발췌' 가 있으면 그 자료의 용어, 정의, 기준을 우선 근거로 쓰고, 근거로 삼은 자료 이름을 content 에 밝힌다.
   학생이 채팅에 첨부한 파일이나 그림이 있으면 그 내용을 보고 답한다.
6. 보고서(docx)도 같은 방식으로 고친다. 한 줄이 문단 하나이고, new_text 의 줄마다 문단이 하나 생긴다.
   제목 문단은 앞에 '# '(1수준), '## '(2수준)를 붙여 보여 준다. new_text 에서도 제목 줄은 같은 표시를 붙이고,
   본문 줄에는 붙이지 않는다. 그 밖의 마크다운 기호(**, -, `)는 쓰지 않고 보고서에 들어갈 문장 그대로 쓴다. '글자를 바꿀 수 없는 줄' 은 지울 때만 범위에 넣는다.
   지워 달라고 하면 지울 범위 전체를 start_line..end_line 로 잡고 new_text 를 빈 문자열로 둔다.
   표를 지우려면 그 표의 모든 행을 범위에 넣는다. 이때는 문단과 여러 표를 한 수정안에서 함께 지워도 된다.
   칸을 빈칸으로 바꾸는 것은 학생이 '내용만 비워 달라' 고 할 때만 한다. '표를 지워 달라' 는 표 자체를 없애는 것이다.
   표는 한 줄이 한 행이고 칸을 ' | ' 로 잇는다. 표 행을 고치거나 더할 때는 new_text 의 줄마다 같은 칸 수로 '칸 | 칸' 모양을 지킨다.
   표 행을 더하려면 그 표의 마지막 행 다음 줄 번호를 start_line 으로, end_line = start_line - 1 로 둔다.
   지우기가 아니면 한 수정안에서 일반 문단과 표 행을 함께 바꾸지 않는다. 둘 다 필요하면 하나만 내고 나머지는 content 에서 다음에 하자고 말한다.
   보고서를 써 달라거나 다듬어 달라고 하면 수정안으로 내고, 의견만 물으면 has_edit 는 false 다.
7. 보고서를 새로 쓰거나 통째로 다시 써 달라고 하면 보고서 첫 줄부터 마지막 줄(표가 있으면 표 앞까지)을 한 수정안으로 바꾼다.
   학생이 원하는 분량을 먼저 따른다.
   - '뼈대', '틀', '목차', '구성만', '내가 채울게' 처럼 말하면 제목 줄과, 제목마다 그 절에 무엇을 쓸지 알려 주는 안내 한두 줄만 쓴다.
     안내 줄은 대괄호로 쓴다. 예: [quantum 2, 4, 8 에서 평균 대기 시간과 문맥 교환 횟수를 표로 정리하고 차이를 해석한다]
     완성된 문장으로 본문을 채우지 않는다.
   - '초안', '써 줘' 처럼 글을 원하면 제목마다 본문을 2~3 문단 쓴다. 한 문단은 3~4 문장이다.
   이미 있는 제목(절 구성)은 살리고, 제목이 없으면 과제 설명과 과제 문제 파일에 맞춰 절을 만든다.
   교과서 일반론을 늘어놓지 말고 이 과제에 맞춰 쓴다. 과제 설명, 과제 문제 파일, 학습 자료, 시작 코드와 테스트가 확인하는 것을 근거로 한다.
   학생이 하지 않은 일을 한 것처럼 쓰지 않는다. '구현했다', '테스트를 모두 통과했다' 는 마지막 실행 결과에 그 근거가 있을 때만 쓴다.
   실행하지 않은 결과나 수치는 지어내지 않고 [실행 결과: …] 처럼 학생이 채울 자리로 남긴다.
   이름, 학번 같은 개인 정보는 [이름], [학번] 자리로 둔다. content 에는 무엇을 썼는지 한두 문장만 쓴다.
"""


def _numbered(content: str, limit: int) -> str:
    lines = content.split("\n")
    body = "\n".join(f"{i + 1:>4}| {line}" for i, line in enumerate(lines))
    return body if len(body) <= limit else body[:limit] + "\n…(이하 생략)"


def _files_section(files: list[WorkspaceFile], focus: str) -> str:
    budget = settings.ai_max_context_chars * 2
    ordered = sorted(files, key=lambda f: (f.path != focus, f.path))
    parts = []
    for f in ordered:
        if f.kind == "doc":
            locked, tables, heads = _doc_info(f)
            shown = "\n".join(("#" * heads[i + 1] + " " if i + 1 in heads else "") + line for i, line in enumerate(f.content.split("\n")))
            note = "".join(f"\n표: {a}–{b}줄, 칸 {c}개" for a, b, c in tables)
            note += f"\n글자를 바꿀 수 없는 줄(그림, 수식, 목차, 병합된 칸이 있는 표. 통째로 지우는 것은 된다): {_ranges(locked)}" if locked else ""
            tag = " (교수가 준 시작 파일)" if f.template else " (학생이 만든 파일)"
            tag += " (읽기 전용)" if f.readonly else ""
            parts.append(f"### {f.path}{tag} [보고서 docx, 한 줄이 문단 하나]{note}\n```\n{_numbered(shown, 12000) if f.content else '(비어 있음)'}\n```")
            continue
        share = max(2000, budget // max(1, len(files)))
        tag = (" (교수가 준 시작 파일" + (", 읽기 전용)" if f.readonly else ")")) if f.template else " (학생이 만든 파일)"
        parts.append(f"### {f.path}{tag} [{f.language}]\n```\n{_numbered(f.content, share)}\n```")
    return "## 작업공간 파일\n" + "\n\n".join(parts)


def _doc_info(f: WorkspaceFile) -> tuple[list[int], list[tuple[int, int, int]], dict[int, int]]:
    path = doc_path(f)
    if not path.exists():
        return [], [], {}
    try:
        data = path.read_bytes()
        return locked_lines(data), table_lines(data), heading_levels(data)
    except Exception:  # noqa: BLE001  깨진 파일이면 알려 줄 것이 없다. 적용할 때 다시 검사한다
        return [], [], {}


def _ranges(nums: list[int]) -> str:
    out: list[str] = []
    for n in nums:
        if out and out[-1].split("–")[-1] == str(n - 1):
            out[-1] = f"{out[-1].split('–')[0]}–{n}"
        else:
            out.append(str(n))
    return ", ".join(out)


@lru_cache(maxsize=32)
def _problem_text(path: str, mtime: float) -> str:  # noqa: ARG001  mtime 은 파일이 바뀌면 다시 뽑게 하는 캐시 키다
    return extract_file_text(Path(path))


def _problem_section(enrollment: Enrollment) -> str:
    """교수가 '과제 문제' 에 첨부한 파일. 과제 설명과 함께 이 과제의 요구사항 원문이다."""
    a = enrollment.assignment
    path = Path(a.attachment_path) if a.attachment_path else None
    if path is None or not path.exists():
        return ""
    text = _problem_text(str(path), path.stat().st_mtime).strip()
    if not text:
        return f"## 교수가 올린 과제 문제 파일: {a.attachment_name}\n(글자를 읽을 수 없는 파일이다. 학생에게 파일을 열어 보라고 안내한다)"
    body = text[:15000] + ("\n…(이하 생략)" if len(text) > 15000 else "")
    return f"## 교수가 올린 과제 문제 파일: {a.attachment_name}\n{body}"


def _materials_section(enrollment: Enrollment, query: str) -> str:
    """교수가 올린 학습 자료에서 질문과 관련된 부분. 학습 단계와 같은 BM25 검색을 쓴다."""
    mats = [(m.filename, m.extracted_text) for m in enrollment.assignment.materials]
    context, has = build_context(mats, query, max_chars=8000)
    if not has:
        return ""
    return "## 학습 자료 발췌 (교수가 올린 자료)\n" + context


def _attachments_section(attempt: TaskAttempt, current: list[ChatAttachment], query: str) -> str:
    """학생이 채팅에 붙인 파일. 이번 메시지 것은 길게 넣고, 앞선 메시지 것은 질문과 관련된 부분을 찾아 넣는다.

    앞선 첨부는 학습 자료와 같은 BM25 검색(build_context)을 쓴다. 다 합쳐 짧으면 전부, 길면 관련 조각만 들어간다.
    그림은 보낸 그 메시지에서만 본다.
    """
    ids = {a.id for a in current}
    earlier = [a for a in attempt.chat_attachments if a.message_id is not None and a.id not in ids]
    parts: list[str] = []
    budget = 12000
    for a in current:
        if a.kind == "image":
            parts.append(f"### {a.filename} (그림, 이 메시지에 함께 보냄)")
            continue
        text = a.extracted_text.strip()
        share = max(2000, budget // max(1, len(current)))
        body = text[:share] + ("\n…(이하 생략)" if len(text) > share else "") if text else "(글자를 읽지 못했다. 스캔한 PDF 일 수 있다)"
        parts.append(f"### {a.filename} (이번 메시지에 첨부)\n```\n{body}\n```")
    found, has = build_context([(a.filename, a.extracted_text) for a in earlier if a.kind == "text"], query, max_chars=6000)
    if has:
        parts.append("### 앞선 메시지에 첨부한 파일에서 이 질문과 관련된 부분\n" + found)
    images = [a.filename for a in earlier if a.kind == "image"]
    if images:
        parts.append(f"(앞선 메시지에 붙인 그림 {', '.join(images[-5:])} 은 지금 볼 수 없다. 필요하면 다시 붙여 달라고 한다)")
    return "## 학생이 채팅에 첨부한 파일\n" + "\n\n".join(parts) if parts else ""


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
    attachments: list[ChatAttachment] | None = None,
) -> WorkspaceReply:
    a = enrollment.assignment
    current = list(attachments or [])
    query = " ".join([message, str((selection or {}).get("text", ""))[:500], focus_path])
    system = "\n\n".join(
        s
        for s in [
            ROLE,
            _rules_section(a),
            _assignment_section(a),
            f"마감: {a.deadline:%Y-%m-%d %H:%M} (UTC)" if a.deadline else "",
            _problem_section(enrollment),
            _materials_section(enrollment, query),
            _files_section(files, focus_path),
            _attachments_section(attempt, current, query),
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
    if current:
        user = f"[첨부: {', '.join(x.filename for x in current)}]\n{user}"
    messages.append({"role": "user", "content": user})
    merged = _merge_consecutive(messages)
    images = [(x.mime, Path(x.path).read_bytes()) for x in current if x.kind == "image" and Path(x.path).exists()]
    if images and merged:
        merged[-1] = {**merged[-1], "images": images}
    return ai_client.parse(
        purpose="chat_workspace",
        system=system,
        messages=merged,
        output_model=WorkspaceReply,
        effort=settings.ai_effort_chat,
        max_tokens=12000,
        db=db,
    )


def run_versions(attempt: TaskAttempt) -> list[ArtifactVersion]:
    return [v for v in attempt.versions if v.source == "workspace" and (v.meta or {}).get("author") == "run"]


def tidy_reply(text: str) -> str:
    """AI 가 소제목과 목록을 줄바꿈 없이 한 줄로 이어 쓰면 줄을 나눠 화면에서 목록으로 보이게 한다."""
    import re

    if "\n" in text or "### " not in text:
        return text
    text = re.sub(r"\s*(#{2,4} )", r"\n\n\1", text).strip()
    # 소제목 줄에서 제목과 본문을 가른다: '### 과제 목표 Round Robin…' 은 제목 뒤 첫 문장부터 본문으로 본다
    lines = []
    for line in text.split("\n"):
        if line.startswith("#"):
            head, _, rest = line.partition(" - ")
            lines.append(head)
            if rest:
                lines.extend("- " + item.strip() for item in rest.split(" - ") if item.strip())
        else:
            lines.append(line)
    return "\n".join(lines)
