"""자리 4 작업공간 API.

커밋 시점은 프런트가 정한다: AI 패널로 포커스 이동, 파일 전환, 실행 직전, AI 요청 직전, 30초 무입력, 5분마다 강제.
서버는 내용이 바뀌었을 때만 커밋(ArtifactVersion, source="workspace")을 만든다.
AI 수정안 수락은 AI 커밋 하나, 실행은 실행 커밋 하나다. 거절은 커밋 없이 이벤트로만 남는다.

교수가 과제를 만들 때 tool_config 에 넣는 값:
    {"files": [{"path": "scheduler.py", "language": "python", "content": "...", "readonly": false}],
     "tests": [{"name": "예제 1", "stdin": "3\\n", "expected": "6"}]}   # tests 는 C++, Java 처럼 입력과 출력으로 채점할 때
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..ai.client import AIUnavailable, ai_client
from ..deps import DB, MyEnrollment
from ..models import AIProposal, ArtifactVersion, ChatMessage, Enrollment, TaskAttempt, TaskEvent, WorkspaceFile
from ..schemas import ChatMessageOut
from ..util import utcnow
from ..workspace import ai as ws_ai
from ..workspace import envs, runner
from ..workspace.diff import line_diff, replace_lines, slice_lines
from ..workspace.docx_store import doc_path, ensure_doc, save_doc
from .common import ai_error, deadline_passed, message_out
from .tasks import current_attempt, ensure_first_attempt, require_open_attempt, require_task_stage

router = APIRouter(prefix="/api/enrollments/{enrollment_id}/workspace", tags=["workspace"])

LANG_BY_EXT = {".py": "python", ".cpp": "cpp", ".cc": "cpp", ".h": "cpp", ".hpp": "cpp", ".java": "java"}
DOC_EXT = {".docx"}
Author = Literal["human", "paste"]


# ---------- 스키마 ----------
class FileOut(BaseModel):
    path: str
    kind: str
    language: str
    content: str
    template: bool
    readonly: bool


class CommitOut(BaseModel):
    id: int
    version_no: int
    file: str
    title: str
    author: str
    trigger: str
    plus: int
    minus: int
    diff: list[dict[str, Any]]
    meta: dict[str, Any]
    created_at: datetime


class ProposalOut(BaseModel):
    id: int
    message_id: int | None
    file_path: str
    start_line: int
    end_line: int
    old_text: str
    new_text: str
    reason: str
    prompt: str
    status: str
    created_at: datetime


class WorkspaceOut(BaseModel):
    enrollment_id: int
    stage: str
    title: str
    subject: str
    description: str
    deadline: datetime | None
    deadline_passed: bool
    editable: bool
    attempt_no: int
    attempt_status: str
    files: list[FileOut]
    commits: list[CommitOut]
    proposals: list[ProposalOut]
    messages: list[ChatMessageOut]
    tests: list[dict[str, Any]]
    last_run: dict[str, Any] | None
    ai_available: bool
    environment: dict[str, Any]


class CommitIn(BaseModel):
    path: str
    content: str
    author: Author = "human"
    trigger: str = Field(default="manual", max_length=30)
    paste: dict[str, Any] | None = None


class CommitResult(BaseModel):
    committed: bool
    commit: CommitOut | None = None
    file: FileOut


class NewFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=200)


class EventIn(BaseModel):
    kind: str = Field(max_length=30)
    path: str = ""
    payload: dict[str, Any] = {}


class PasteSourceIn(BaseModel):
    source: str | None = None
    memo: str = Field(default="", max_length=500)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    path: str = ""
    selection: dict[str, Any] | None = None


class ChatOut(BaseModel):
    messages: list[ChatMessageOut]
    proposal: ProposalOut | None


class AcceptIn(BaseModel):
    text: str | None = None  # 학생이 고친 내용. 없으면 AI 수정안 그대로


class ProposalResult(BaseModel):
    proposal: ProposalOut
    file: FileOut | None = None
    commit: CommitOut | None = None


class RunIn(BaseModel):
    path: str
    mode: Literal["run", "test"] = "run"
    stdin: str = Field(default="", max_length=100_000)
    env: Literal["default", "professor"] | None = None  # 없으면 학생이 고른 환경. 제출 전 확인은 professor


class RunOut(BaseModel):
    result: dict[str, Any]
    commit: CommitOut


# ---------- 변환 ----------
def file_out(f: WorkspaceFile) -> FileOut:
    return FileOut(path=f.path, kind=f.kind, language=f.language, content=f.content, template=f.template, readonly=f.readonly)


def commit_out(v: ArtifactVersion) -> CommitOut:
    m = v.meta or {}
    return CommitOut(
        id=v.id,
        version_no=v.version_no,
        file=v.file_name,
        title=v.reason,
        author=str(m.get("author", "human")),
        trigger=str(m.get("trigger", "")),
        plus=int(m.get("plus", 0)),
        minus=int(m.get("minus", 0)),
        diff=list(m.get("diff", [])),
        meta={k: val for k, val in m.items() if k not in ("diff",)},
        created_at=v.created_at,
    )


def proposal_out(p: AIProposal) -> ProposalOut:
    return ProposalOut.model_validate(p, from_attributes=True)


# ---------- 공통 ----------
def _lang(path: str) -> str:
    return LANG_BY_EXT.get(PurePosixPath(path).suffix.lower(), "")


def _kind(path: str) -> str:
    return "doc" if PurePosixPath(path).suffix.lower() in DOC_EXT else "code"


def _clean_path(raw: str) -> str:
    p = PurePosixPath(raw.strip().lstrip("/"))
    if (
        not raw.strip()
        or raw.strip().endswith("/")
        or any(part in ("", ".", "..") or part.startswith("__") or part.strip() != part for part in raw.lstrip("/").split("/"))
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "파일 경로가 올바르지 않다")
    return p.as_posix()


def _check_free(attempt: TaskAttempt, path: str, ignore: set[str] | None = None) -> None:
    """같은 경로의 파일이 있거나, 파일과 폴더 이름이 겹치면 막는다."""
    ignore = ignore or set()
    for f in attempt.workspace_files:
        if f.path in ignore:
            continue
        if f.path == path:
            raise HTTPException(status.HTTP_409_CONFLICT, f"{path} 이(가) 이미 있다")
        if f.path.startswith(path + "/") or path.startswith(f.path + "/"):
            raise HTTPException(status.HTTP_409_CONFLICT, "같은 이름의 파일과 폴더를 함께 둘 수 없다")


def _ensure_workspace(db: Session, e: Enrollment, attempt: TaskAttempt) -> None:
    """시도의 첫 접근이면 파일을 만든다. 재제출 시도는 직전 시도의 마지막 내용에서 이어서 시작한다."""
    if attempt.workspace_files:
        return
    prev = [t for t in e.task_attempts if t.attempt_no < attempt.attempt_no and t.workspace_files]
    if prev:
        source = [(f.path, f.kind, f.language, f.content, f.template, f.readonly) for f in prev[-1].workspace_files]
        how = f"시도 {prev[-1].attempt_no}에서 이어서 시작"
    else:
        tpl = (e.assignment.tool_config or {}).get("files", [])
        source = [
            (
                _clean_path(str(t["path"])),
                _kind(str(t["path"])),
                str(t.get("language") or _lang(str(t["path"]))),
                str(t.get("content", "")),
                True,
                bool(t.get("readonly", False)),
            )
            for t in tpl
        ]
        how = "과제 템플릿으로 작업공간 생성"
    prev_docs = {f.path: f for f in prev[-1].workspace_files} if prev else {}
    for path, kind, language, content, template, readonly in source:
        wf = WorkspaceFile(attempt_id=attempt.id, path=path, kind=kind, language=language, content=content, template=template, readonly=readonly)
        db.add(wf)
        if kind == "doc":
            db.flush()
            old_doc = prev_docs.get(path)
            if old_doc is not None and doc_path(old_doc).exists():
                wf.content = save_doc(wf, doc_path(old_doc).read_bytes())
            else:
                ensure_doc(wf, outline=content)  # 템플릿의 content 는 보고서 개요로 쓴다
    db.add(TaskEvent(attempt_id=attempt.id, kind="workspace_init", payload={"how": how, "files": [s[0] for s in source]}))
    db.commit()
    db.refresh(attempt)


def _open(db: Session, e: Enrollment) -> TaskAttempt:
    if e.assignment.tool_name != "workspace":
        raise HTTPException(status.HTTP_409_CONFLICT, "이 과제는 작업공간 도구를 쓰지 않는다")
    attempt = require_open_attempt(e)
    _ensure_workspace(db, e, attempt)
    return attempt


def _file(attempt: TaskAttempt, path: str) -> WorkspaceFile:
    for f in attempt.workspace_files:
        if f.path == path:
            return f
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"파일 {path} 이 없다")


def _next_version_no(db: Session, attempt: TaskAttempt) -> int:
    n = db.query(func.max(ArtifactVersion.version_no)).filter(ArtifactVersion.attempt_id == attempt.id).scalar()
    return int(n or 0) + 1


def make_commit(
    db: Session,
    attempt: TaskAttempt,
    f: WorkspaceFile,
    new_content: str,
    *,
    author: str,
    trigger: str,
    title: str = "",
    extra: dict[str, Any] | None = None,
) -> ArtifactVersion | None:
    """내용이 바뀌었을 때만 커밋한다."""
    if f.content == new_content:
        return None
    diff, plus, minus = line_diff(f.content, new_content)
    if not title:
        label = {"human": "직접 수정", "paste": "붙여넣기", "ai": "AI 수정안 수락"}.get(author, author)
        title = f"{f.path} {label}"
    f.content = new_content
    f.updated_at = utcnow()
    v = ArtifactVersion(
        attempt_id=attempt.id,
        version_no=_next_version_no(db, attempt),
        source="workspace",
        kind="text",
        text_content=new_content,
        file_name=f.path,
        reason=title,
        meta={"author": author, "trigger": trigger, "file": f.path, "plus": plus, "minus": minus, "diff": diff, **(extra or {})},
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _workspace_out(db: Session, e: Enrollment) -> WorkspaceOut:
    a = e.assignment
    attempt = current_attempt(e)
    editable = attempt is not None and not deadline_passed(a)
    if attempt is None and e.task_attempts:
        attempt = e.task_attempts[-1]
    if attempt is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "과제 시도가 없다")
    if editable:
        _ensure_workspace(db, e, attempt)
    versions = [v for v in attempt.versions if v.source == "workspace"]
    runs = ws_ai.run_versions(attempt)
    tests = (a.tool_config or {}).get("tests", [])
    return WorkspaceOut(
        enrollment_id=e.id,
        stage=e.stage,
        title=a.title,
        subject=a.subject,
        description=a.description,
        deadline=a.deadline,
        deadline_passed=deadline_passed(a),
        editable=editable,
        attempt_no=attempt.attempt_no,
        attempt_status=attempt.status,
        files=[file_out(f) for f in attempt.workspace_files],
        commits=[commit_out(v) for v in reversed(versions)],
        proposals=[proposal_out(p) for p in attempt.proposals],
        messages=[message_out(m) for m in e.messages if m.stage == "task" and m.attempt_no == attempt.attempt_no],
        tests=[{"name": t.get("name", "")} for t in tests],
        last_run=(runs[-1].meta or {}).get("run") if runs else None,
        ai_available=ai_client.available,
        environment=envs.env_info(db, e),
    )


# ---------- 엔드포인트 ----------
@router.get("", response_model=WorkspaceOut)
def get_workspace(e: MyEnrollment, db: DB) -> WorkspaceOut:
    require_task_stage(e)
    if e.assignment.tool_name != "workspace":
        raise HTTPException(status.HTTP_409_CONFLICT, "이 과제는 작업공간 도구를 쓰지 않는다")
    ensure_first_attempt(db, e)
    return _workspace_out(db, e)


@router.post("/files", response_model=FileOut, status_code=status.HTTP_201_CREATED)
def new_file(body: NewFileIn, e: MyEnrollment, db: DB) -> FileOut:
    attempt = _open(db, e)
    path = _clean_path(body.path)
    _check_free(attempt, path)
    f = WorkspaceFile(attempt_id=attempt.id, path=path, kind=_kind(path), language=_lang(path), content="")
    db.add(f)
    if f.kind == "doc":
        db.flush()
        ensure_doc(f)
    db.add(TaskEvent(attempt_id=attempt.id, file_path=path, kind="file_create"))
    db.commit()
    db.refresh(f)
    return file_out(f)


class RenameIn(BaseModel):
    path: str
    new_path: str
    folder: bool = False  # true 면 path 폴더 아래 파일을 모두 옮긴다


class DeleteIn(BaseModel):
    path: str
    folder: bool = False


class FileOpResult(BaseModel):
    files: list[FileOut]
    commits: list[CommitOut]


def _targets(attempt: TaskAttempt, path: str, folder: bool) -> list[WorkspaceFile]:
    if folder:
        found = [f for f in attempt.workspace_files if f.path.startswith(path.rstrip("/") + "/")]
    else:
        found = [f for f in attempt.workspace_files if f.path == path]
    if not found:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{path} 이(가) 없다")
    locked = [f.path for f in found if f.template]
    if locked:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "교수가 준 기본 파일은 옮기거나 지울 수 없다: " + ", ".join(locked))
    return found


def _file_op_commit(db: Session, attempt: TaskAttempt, f: WorkspaceFile, title: str, trigger: str, diff: list[dict[str, Any]], minus: int, extra: dict[str, Any]) -> ArtifactVersion:
    v = ArtifactVersion(
        attempt_id=attempt.id,
        version_no=_next_version_no(db, attempt),
        source="workspace",
        kind="text",
        text_content=f.content if trigger != "delete" else None,
        file_name=f.path,
        reason=title,
        meta={"author": "human", "trigger": trigger, "file": f.path, "plus": 0, "minus": minus, "diff": diff, **extra},
    )
    db.add(v)
    db.flush()
    return v


@router.post("/files/rename", response_model=FileOpResult)
def rename_file(body: RenameIn, e: MyEnrollment, db: DB) -> FileOpResult:
    attempt = _open(db, e)
    src = _clean_path(body.path)
    dst = _clean_path(body.new_path)
    if src == dst:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "이름이 같다")
    found = _targets(attempt, src, body.folder)
    moves = [(f, dst + f.path[len(src):] if body.folder else dst) for f in found]
    moving = {f.path for f in found}
    for _, new in moves:
        _check_free(attempt, new, ignore=moving)
    if not body.folder and _kind(dst) != found[0].kind:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "코드 파일과 보고서 사이로는 확장자를 바꿀 수 없다")
    out: list[ArtifactVersion] = []
    for f, new in moves:
        old = f.path
        f.path = new
        f.language = _lang(new) if f.kind == "code" else ""
        for p in attempt.proposals:
            if p.file_path == old:
                p.file_path = new
        db.add(TaskEvent(attempt_id=attempt.id, file_path=new, kind="file_rename", payload={"from": old, "to": new}))
        out.append(_file_op_commit(db, attempt, f, f"이름 변경: {old} → {new}", "rename", [], 0, {"renamed_from": old}))
    db.commit()
    return FileOpResult(files=[file_out(f) for f, _ in moves], commits=[commit_out(v) for v in reversed(out)])


@router.post("/files/delete", response_model=FileOpResult)
def delete_file(body: DeleteIn, e: MyEnrollment, db: DB) -> FileOpResult:
    attempt = _open(db, e)
    path = _clean_path(body.path)
    found = _targets(attempt, path, body.folder)
    out: list[ArtifactVersion] = []
    for f in found:
        lines = f.content.split("\n") if f.content else []
        diff = [{"type": "del", "no": i + 1, "text": t} for i, t in enumerate(lines[:400])]
        for p in attempt.proposals:
            if p.file_path == f.path and p.status == "pending":
                p.status = "stale"
                p.resolved_at = utcnow()
        db.add(TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="file_delete", payload={"chars": len(f.content)}))
        out.append(_file_op_commit(db, attempt, f, f"파일 삭제: {f.path}", "delete", diff, len(lines), {"deleted_text": f.content[:20000]}))
        db.delete(f)
    db.commit()
    return FileOpResult(files=[], commits=[commit_out(v) for v in reversed(out)])


@router.post("/commit", response_model=CommitResult)
def commit(body: CommitIn, e: MyEnrollment, db: DB) -> CommitResult:
    attempt = _open(db, e)
    f = _file(attempt, body.path)
    if f.readonly:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "읽기 전용 파일이다")
    if f.kind == "doc":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "보고서는 편집기 저장으로 커밋된다")
    extra: dict[str, Any] = {}
    title = ""
    if body.author == "paste":
        paste = dict(body.paste or {})
        extra["paste"] = {"source": paste.get("source"), "memo": paste.get("memo", ""), "chars": int(paste.get("chars", 0)), "lines": int(paste.get("lines", 0))}
        db.add(TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="paste", payload=extra["paste"]))
        title = f"{f.path} 붙여넣기 {extra['paste']['lines']}줄"
    v = make_commit(db, attempt, f, body.content, author=body.author, trigger=body.trigger, title=title, extra=extra)
    if v is None:
        db.commit()
    return CommitResult(committed=v is not None, commit=commit_out(v) if v else None, file=file_out(f))


@router.post("/commits/{version_id}/paste-source", response_model=CommitOut)
def paste_source(version_id: int, body: PasteSourceIn, e: MyEnrollment, db: DB) -> CommitOut:
    attempt = _open(db, e)
    v = db.get(ArtifactVersion, version_id)
    if v is None or v.attempt_id != attempt.id or "paste" not in (v.meta or {}):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "붙여넣기 커밋이 없다")
    meta = dict(v.meta)
    meta["paste"] = {**meta["paste"], "source": body.source, "memo": body.memo}
    v.meta = meta
    if body.source:
        v.reason = f"{v.file_name} 붙여넣기 · {body.source}"
    db.commit()
    db.refresh(v)
    return commit_out(v)


@router.post("/events", status_code=status.HTTP_204_NO_CONTENT)
def add_events(body: list[EventIn], e: MyEnrollment, db: DB) -> None:
    attempt = _open(db, e)
    for ev in body[:100]:
        db.add(TaskEvent(attempt_id=attempt.id, file_path=ev.path, kind=ev.kind, payload=ev.payload))
    db.commit()


@router.post("/chat", response_model=ChatOut)
def chat(body: ChatIn, e: MyEnrollment, db: DB) -> ChatOut:
    attempt = _open(db, e)
    text = body.message.strip()
    student = ChatMessage(
        enrollment_id=e.id, stage="task", attempt_no=attempt.attempt_no, role="student", content=text,
        meta={"workspace": True, "path": body.path, "selection": body.selection},
    )  # fmt: skip
    db.add(student)
    db.add(TaskEvent(attempt_id=attempt.id, file_path=body.path, kind="ai_request", payload={"selection": body.selection, "chars": len(text)}))
    db.flush()
    try:
        r = ws_ai.reply(db, e, attempt, list(attempt.workspace_files), text, body.path, body.selection)
    except AIUnavailable as exc:
        db.rollback()
        raise ai_error(exc) from exc

    proposal: AIProposal | None = None
    note = ""
    if r.has_edit and r.edit_path:
        target = next((f for f in attempt.workspace_files if f.path == r.edit_path), None)
        n_lines = len(target.content.split("\n")) if target else 0
        valid = (
            target is not None
            and not target.readonly
            and target.kind == "code"
            and 1 <= r.start_line <= n_lines + 1
            and r.start_line - 1 <= r.end_line <= n_lines
        )
        if valid and target is not None:
            proposal = AIProposal(
                attempt_id=attempt.id,
                file_path=target.path,
                start_line=r.start_line,
                end_line=r.end_line,
                old_text=slice_lines(target.content, r.start_line, r.end_line) if r.end_line >= r.start_line else "",
                new_text=r.new_text.rstrip("\n"),
                reason=r.edit_reason.strip(),
                prompt=text,
            )
        else:
            note = "\n\n(수정안의 위치가 파일과 맞지 않아 적용할 수 없었다. 다시 요청해 주세요.)"

    ai_msg = ChatMessage(
        enrollment_id=e.id, stage="task", attempt_no=attempt.attempt_no, role="ai",
        content=r.content.strip() + note, mode="workspace", question_kind="",
        meta={"workspace": True, "has_edit": proposal is not None},
    )  # fmt: skip
    db.add(ai_msg)
    db.flush()
    if proposal is not None:
        proposal.message_id = ai_msg.id
        db.add(proposal)
        db.flush()
        ai_msg.meta = {**ai_msg.meta, "proposal_id": proposal.id}
    db.commit()
    db.refresh(student)
    db.refresh(ai_msg)
    if proposal is not None:
        db.refresh(proposal)
    return ChatOut(messages=[message_out(student), message_out(ai_msg)], proposal=proposal_out(proposal) if proposal else None)


def _pending(db: Session, attempt: TaskAttempt, proposal_id: int) -> AIProposal:
    p = db.get(AIProposal, proposal_id)
    if p is None or p.attempt_id != attempt.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "수정안이 없다")
    if p.status != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 처리한 수정안이다")
    return p


@router.post("/proposals/{proposal_id}/accept", response_model=ProposalResult)
def accept(proposal_id: int, body: AcceptIn, e: MyEnrollment, db: DB) -> ProposalResult:
    attempt = _open(db, e)
    p = _pending(db, attempt, proposal_id)
    f = _file(attempt, p.file_path)
    start, end = p.start_line, p.end_line
    if end >= start and slice_lines(f.content, start, end) != p.old_text:
        # 수정안을 받은 뒤 파일이 바뀌었다. 같은 원문이 한 곳에만 있으면 그 자리로 옮긴다
        lines = f.content.split("\n")
        old = p.old_text.split("\n")
        hits = [i for i in range(len(lines) - len(old) + 1) if lines[i : i + len(old)] == old]
        if len(hits) != 1:
            p.status = "stale"
            p.resolved_at = utcnow()
            db.commit()
            raise HTTPException(status.HTTP_409_CONFLICT, "수정안을 받은 뒤 파일이 바뀌어 적용할 수 없다. 다시 요청해 주세요")
        start, end = hits[0] + 1, hits[0] + len(old)
    edited = body.text is not None and body.text.rstrip("\n") != p.new_text
    new_text = body.text.rstrip("\n") if edited and body.text is not None else p.new_text
    new_content = replace_lines(f.content, start, end, new_text)
    extra = {"proposal_id": p.id, "prompt": p.prompt, "reason": p.reason, "message_id": p.message_id, "edited_from_ai": edited}
    if edited:
        extra["ai_text"] = p.new_text
    v = make_commit(
        db, attempt, f, new_content,
        author="human" if edited else "ai",
        trigger="ai_accept",
        title=("AI 수정안을 고쳐서 반영: " if edited else "AI: ") + (p.reason or f.path),
        extra=extra,
    )  # fmt: skip
    p.status = "edited" if edited else "accepted"
    p.resolved_at = utcnow()
    db.add(TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="ai_edit" if edited else "ai_accept", payload={"proposal_id": p.id}))
    db.commit()
    db.refresh(p)
    return ProposalResult(proposal=proposal_out(p), file=file_out(f), commit=commit_out(v) if v else None)


@router.post("/proposals/{proposal_id}/reject", response_model=ProposalResult)
def reject(proposal_id: int, e: MyEnrollment, db: DB) -> ProposalResult:
    attempt = _open(db, e)
    p = _pending(db, attempt, proposal_id)
    p.status = "rejected"
    p.resolved_at = utcnow()
    db.add(TaskEvent(attempt_id=attempt.id, file_path=p.file_path, kind="ai_reject", payload={"proposal_id": p.id}))
    db.commit()
    db.refresh(p)
    return ProposalResult(proposal=proposal_out(p))


@router.post("/run", response_model=RunOut)
def run_code(body: RunIn, e: MyEnrollment, db: DB) -> RunOut:
    attempt = _open(db, e)
    f = _file(attempt, body.path)
    language = f.language or _lang(f.path)
    if language not in ("python", "cpp", "java"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "실행할 수 없는 파일이다. Python, C++, Java 만 실행한다")
    files = {x.path: x.content for x in attempt.workspace_files if x.kind == "code"}
    tests = list((e.assignment.tool_config or {}).get("tests", []))
    try:
        env_key, image, cflags, env_os = envs.resolve(db, e, body.env)
    except envs.EnvError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    try:
        result = runner.run(files, language, f.path, body.mode, body.stdin, tests, image=image, cflags=cflags)
        result.update(env=env_key, env_os=env_os)
    except runner.RunUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    if result.get("compile_error"):
        title = "컴파일 오류"
    elif body.mode == "test" and result.get("error"):
        title = f"테스트 실행 실패 · {result['error']}"
    elif body.mode == "test":
        title = f"테스트 실행 · {result['passed']}/{result['total']} 통과"
    else:
        title = "실행 · " + ("시간 초과" if result.get("timed_out") else f"종료 코드 {result.get('exit_code')}")
    if body.env == "professor":
        title = f"교수 환경 확인 · {title}"
    stored = {**result, "stdout": str(result.get("stdout", ""))[:4000], "stdin": body.stdin[:2000]}
    v = ArtifactVersion(
        attempt_id=attempt.id,
        version_no=_next_version_no(db, attempt),
        source="workspace",
        kind="text",
        text_content=None,
        file_name=f.path,
        reason=title,
        meta={"author": "run", "trigger": body.mode, "file": f.path, "plus": 0, "minus": 0, "run": stored},
    )
    db.add(v)
    db.add(TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="compile" if result.get("compile_error") else "run", payload={"mode": body.mode, "passed": result.get("passed"), "total": result.get("total")}))
    db.commit()
    db.refresh(v)
    return RunOut(result=result, commit=commit_out(v))
