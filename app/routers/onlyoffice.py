"""자리 4 보고서(docx) 편집: ONLYOFFICE Document Server 연동.

흐름
1. 프런트가 /doc-config 로 편집기 설정을 받아 Docs API 로 편집기를 띄운다. 설정은 JWT 로 서명한다.
2. Document Server 가 /api/onlyoffice/files/{id} 에서 docx 를 받아 연다.
3. 저장 시점마다 Document Server 가 /api/onlyoffice/callback/{id} 를 부른다.
   status 6 = 강제 저장(편집 중), status 2 = 편집 세션이 끝나 저장. 둘 다 저장본을 받아 텍스트를 뽑고 바뀌었으면 커밋한다.
4. 우리 쪽 커밋 시점(AI 패널로 포커스 이동, 파일 전환, 30초 등)에는 프런트가 /doc-forcesave 를 부르고,
   백엔드가 Document Server 명령 서비스에 forcesave 를 보낸 뒤 콜백으로 커밋이 생길 때까지 잠깐 기다린다.

ONLYOFFICE 안의 입력은 키 단위로 받을 수 없으므로 사람 수정 기록은 저장(커밋) 단위다.
"""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import func

from ..config import settings
from ..db import SessionLocal
from ..deps import DB, MyEnrollment
from ..models import ArtifactVersion, TaskEvent, WorkspaceFile
from ..workspace.docx_store import ensure_doc, save_doc
from .workspace import CommitOut, _file, _open, commit_out, make_commit

router = APIRouter(tags=["onlyoffice"])

FILE_TOKEN_SECONDS = 60 * 60 * 12


def configured() -> bool:
    return bool(settings.onlyoffice_jwt_secret)


def _secret() -> str:
    if not configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ONLYOFFICE_JWT_SECRET 이 없어 보고서 편집기를 쓸 수 없다")
    return settings.onlyoffice_jwt_secret


def _file_token(file_id: int, purpose: str) -> str:
    """Document Server 가 쓰는 다운로드, 콜백 주소에 붙이는 토큰. 학생 로그인 토큰을 넘기지 않는다."""
    payload = {"fid": file_id, "p": purpose, "exp": int(time.time()) + FILE_TOKEN_SECONDS}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def _check_file_token(token: str, file_id: int, purpose: str) -> None:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "토큰이 유효하지 않다") from exc
    if data.get("fid") != file_id or data.get("p") != purpose:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "토큰이 이 파일 것이 아니다")


def doc_key(f: WorkspaceFile) -> str:
    return f"ws{f.id}r{f.doc_rev}"


# ---------- 학생 화면 ----------
class DocConfigOut(BaseModel):
    api_url: str
    config: dict[str, Any]


@router.get("/api/enrollments/{enrollment_id}/workspace/doc-config", response_model=DocConfigOut)
def doc_config(path: str, e: MyEnrollment, db: DB, theme: str = "light") -> DocConfigOut:
    secret = _secret()
    attempt = _open(db, e)
    f = _file(attempt, path)
    if f.kind != "doc":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "보고서 파일이 아니다")
    tpl = next((t for t in (e.assignment.tool_config or {}).get("files", []) if t.get("path") == f.path), {})
    ensure_doc(f, outline=str(tpl.get("content", "")) if f.template else "")
    db.commit()
    base = settings.backend_url_for_onlyoffice.rstrip("/")
    config: dict[str, Any] = {
        "documentType": "word",
        "document": {
            "fileType": "docx",
            "key": doc_key(f),
            "title": f.path,
            "url": f"{base}/api/onlyoffice/files/{f.id}?t={_file_token(f.id, 'download')}",
            "permissions": {"edit": not f.readonly, "download": True, "print": True},
        },
        "editorConfig": {
            "mode": "view" if f.readonly else "edit",
            "lang": "ko",
            "callbackUrl": f"{base}/api/onlyoffice/callback/{f.id}?t={_file_token(f.id, 'callback')}",
            "user": {"id": str(e.student_id), "name": e.student.name},
            "customization": {
                "forcesave": True,
                "autosave": True,
                "compactHeader": True,
                "toolbarNoTabs": False,
                "hideRightMenu": True,
                "uiTheme": "theme-dark" if theme == "dark" else "theme-light",
            },
        },
        "width": "100%",
        "height": "100%",
    }
    config["token"] = jwt.encode(config, secret, algorithm="HS256")
    return DocConfigOut(api_url=f"{settings.onlyoffice_public_url.rstrip('/')}/web-apps/apps/api/documents/api.js", config=config)


class ForceSaveIn(BaseModel):
    path: str
    trigger: str = "doc_forcesave"
    key: str = ""  # 편집기가 지금 쓰는 문서 키. 세션 도중 키가 바뀌어도 열린 세션에 저장을 요청한다


class ForceSaveOut(BaseModel):
    saved: bool
    commit: CommitOut | None = None
    note: str = ""


def _version_count(db: Any, attempt_id: int) -> int:
    return int(db.query(func.count(ArtifactVersion.id)).filter(ArtifactVersion.attempt_id == attempt_id).scalar() or 0)


@router.post("/api/enrollments/{enrollment_id}/workspace/doc-forcesave", response_model=ForceSaveOut)
def doc_forcesave(body: ForceSaveIn, e: MyEnrollment, db: DB) -> ForceSaveOut:
    secret = _secret()
    attempt = _open(db, e)
    f = _file(attempt, body.path)
    if f.kind != "doc":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "보고서 파일이 아니다")
    before = _version_count(db, attempt.id)
    ev = TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="doc_forcesave", payload={"trigger": body.trigger})
    db.add(ev)
    db.commit()
    start_event = ev.id
    key = body.key if re.fullmatch(rf"ws{f.id}r\d+", body.key or "") else doc_key(f)
    # 브라우저 편집기는 방금 입력한 내용을 조금 늦게 서버로 보낸다. 그래서 '변경 없음'이나 '같은 내용'이면
    # 잠깐 기다렸다가 다시 요청한다. 실제 지연은 1~3초라 4.5초까지만 본다.
    give_up = time.time() + 4.5
    note = "바뀐 내용이 없다"
    while time.time() < give_up:
        code = _send_forcesave(secret, key, body.trigger)
        if code not in (0, 4):
            return ForceSaveOut(saved=False, note=f"ONLYOFFICE 응답 오류 {code}")
        if code == 0:
            got = _wait_callback(attempt.id, f.path, before, start_event, give_up)
            if got == "same":
                note = "저장했지만 글 내용은 바뀌지 않았다(서식만 바뀐 경우 등)"
                with SessionLocal() as s2:
                    start_event = int(s2.query(func.max(TaskEvent.id)).scalar() or start_event)
            elif isinstance(got, CommitOut):
                return ForceSaveOut(saved=True, commit=got)
        time.sleep(1.5)
    return ForceSaveOut(saved=False, note=note)


def _send_forcesave(secret: str, key: str, trigger: str) -> int:
    payload = {"c": "forcesave", "key": key, "userdata": trigger}
    token = jwt.encode(payload, secret, algorithm="HS256")
    try:
        r = httpx.post(
            f"{settings.onlyoffice_internal_url.rstrip('/')}/command",
            json={**payload, "token": token},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return int(r.json().get("error", -1))
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"ONLYOFFICE 에 저장을 요청하지 못했다: {exc}") from exc


def _wait_callback(attempt_id: int, path: str, before: int, start_event: int, give_up: float) -> CommitOut | str | None:
    """저장본은 콜백(status 6)으로 따로 온다. 커밋이 생기면 그 커밋을, 내용이 같았으면 'same' 을 돌려준다."""
    deadline = min(give_up, time.time() + 6)
    while time.time() < deadline:
        time.sleep(0.3)
        with SessionLocal() as s2:
            if _version_count(s2, attempt_id) > before:
                v = (
                    s2.query(ArtifactVersion)
                    .filter(ArtifactVersion.attempt_id == attempt_id, ArtifactVersion.file_name == path)
                    .order_by(ArtifactVersion.version_no.desc())
                    .first()
                )
                return commit_out(v) if v else None
            same = (
                s2.query(TaskEvent)
                .filter(TaskEvent.attempt_id == attempt_id, TaskEvent.kind == "doc_saved_same", TaskEvent.id > start_event)
                .first()
            )
            if same is not None:
                return "same"
    return None


# ---------- Document Server 가 부르는 주소 ----------
@router.get("/api/onlyoffice/files/{file_id}", include_in_schema=False)
def download(file_id: int, t: str, db: DB) -> FileResponse:
    _check_file_token(t, file_id, "download")
    f = db.get(WorkspaceFile, file_id)
    if f is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일이 없다")
    path = ensure_doc(f)
    db.commit()
    return FileResponse(path, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", filename=f.path)


def _internal(url: str) -> str:
    """콜백에 담긴 저장본 주소를 백엔드가 접근할 수 있는 주소로 바꾼다."""
    src = urlsplit(url)
    dst = urlsplit(settings.onlyoffice_internal_url)
    return urlunsplit((dst.scheme, dst.netloc, src.path, src.query, src.fragment))


@router.post("/api/onlyoffice/callback/{file_id}", include_in_schema=False)
async def callback(file_id: int, t: str, request: Request) -> dict[str, int]:
    _check_file_token(t, file_id, "callback")
    secret = _secret()
    body: dict[str, Any] = await request.json()
    token = body.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    try:
        signed = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return {"error": 1}
    data: dict[str, Any] = signed.get("payload", signed)
    state = int(data.get("status", 0))
    if state not in (2, 6):
        return {"error": 0}
    url = data.get("url")
    if not url:
        return {"error": 1}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_internal(str(url)))
    if r.status_code != 200:
        return {"error": 1}
    _save_callback(file_id, r.content, state, str(data.get("userdata") or ""), data)
    return {"error": 0}


def _save_callback(file_id: int, content: bytes, state: int, userdata: str, data: dict[str, Any]) -> None:
    with SessionLocal() as db:
        f = db.get(WorkspaceFile, file_id)
        if f is None:
            return
        from ..models import TaskAttempt

        attempt = db.get(TaskAttempt, f.attempt_id)
        if attempt is None:
            return
        text = save_doc(f, content)
        trigger = userdata or ("doc_close" if state == 2 else "doc_save")
        v = make_commit(db, attempt, f, text, author="human", trigger=trigger, title=f"{f.path} 직접 수정")
        if v is None:
            db.add(TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="doc_saved_same", payload={"key": doc_key(f), "trigger": trigger}))
        if state == 2 and data.get("key") == doc_key(f):
            # 편집 세션이 끝났다. 다음에 열 때는 새 키로 저장본을 다시 받게 한다
            # 이미 지난 키의 세션이 늦게 끝난 경우에는 키를 또 올리지 않는다
            f.doc_rev += 1
        db.add(TaskEvent(attempt_id=attempt.id, file_path=f.path, kind="doc_callback", payload={"status": state, "actions": data.get("actions"), "users": data.get("users")}))
        db.commit()
