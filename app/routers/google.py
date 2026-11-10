"""자리 2 Google 문서 연동.

학생이 Google 계정을 연결하고 문서 주소를 지정하면 서버가 Google Drive API의 revisions 목록을 받아
새 수정 이력마다 텍스트를 내보내 결과물 버전으로 저장한다. 학습 AI는 같은 사이트의 채팅을 문서 탭 위에 띄운다.
문서는 작업 중에 다른 주소로 바꿀 수 있고, 버전마다 어느 문서에서 받은 것인지 meta의 doc_id와 doc_name에 남긴다.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import DB, MyEnrollment
from ..models import ArtifactVersion, Enrollment, GoogleConnection
from ..schemas import TaskStateOut
from ..util import utcnow
from .tasks import current_attempt, require_open_attempt, task_state

router = APIRouter(tags=["google"])

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
DRIVE_ABOUT = "https://www.googleapis.com/drive/v3/about"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DOC_MIME = "application/vnd.google-apps.document"
# 편집 화면 주소, 계정 번호가 낀 주소, Drive 파일 주소, id 매개변수 주소를 차례로 본다
DOC_ID_RES = (
    re.compile(r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)"),
    re.compile(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)"),
    re.compile(r"(?:drive|docs)\.google\.com/\S*[?&]id=([A-Za-z0-9_-]+)"),
)
SCOPE_REASONS = {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "insufficientScopes"}


class DocIn(BaseModel):
    doc_url: str


class SyncOut(BaseModel):
    task: TaskStateOut
    added: int
    doc_id: str


def configured() -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def _require_configured() -> None:
    if not configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Google 연동 키가 서버에 설정되지 않았다. GOOGLE_CLIENT_ID와 GOOGLE_CLIENT_SECRET이 필요하다",
        )


def parse_doc_id(url: str) -> str:
    value = url.strip()
    if "/document/d/e/" in value:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "웹에 게시한 주소로는 문서를 지정할 수 없다. 문서를 편집하는 화면의 주소를 붙여 넣는다",
        )
    for pattern in DOC_ID_RES:
        m = pattern.search(value)
        if m:
            return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Google 문서 주소가 아니다")


def make_state(enrollment_id: int, user_id: int) -> str:
    payload = {"eid": enrollment_id, "uid": user_id, "exp": utcnow() + timedelta(minutes=15), "kind": "google"}
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def read_state(state: str) -> tuple[int, int]:
    try:
        payload = jwt.decode(state, settings.secret_key, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "연결 요청이 만료되었거나 유효하지 않다") from exc
    if payload.get("kind") != "google":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "연결 요청이 유효하지 않다")
    return int(payload["eid"]), int(payload["uid"])


@router.get("/api/enrollments/{enrollment_id}/google/auth-url")
def auth_url(e: MyEnrollment) -> dict[str, str]:
    _require_configured()
    if e.assignment.tool_name != "google_docs":
        raise HTTPException(status.HTTP_409_CONFLICT, "이 과제는 Google 문서를 쓰지 않는다")
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": make_state(e.id, e.student_id),
    }
    return {"url": f"{AUTH_URL}?{urlencode(params)}"}


@router.get("/api/google/callback", include_in_schema=False)
def callback(db: DB, code: str = Query(default=""), state: str = Query(default=""), error: str = Query(default="")):  # type: ignore[no-untyped-def]
    _require_configured()
    eid, uid = read_state(state)
    base = settings.public_url.rstrip("/")
    target = f"{base}/stu/enrollments/{eid}"
    if error or not code:
        return RedirectResponse(f"{target}?google=denied")
    e = db.get(Enrollment, eid)
    if e is None or e.student_id != uid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "참여 정보를 찾을 수 없다")
    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
        "grant_type": "authorization_code",
    }
    r = httpx.post(TOKEN_URL, data=data, timeout=20)
    if r.status_code >= 400:
        return RedirectResponse(f"{target}?google=failed")
    tok = r.json()
    conn = db.query(GoogleConnection).filter(GoogleConnection.enrollment_id == e.id).first()
    if conn is None:
        conn = GoogleConnection(enrollment_id=e.id)
        db.add(conn)
    conn.access_token = tok.get("access_token", "")
    if tok.get("refresh_token"):
        conn.refresh_token = tok["refresh_token"]
    conn.token_expiry = utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)
    conn.connected_at = utcnow()
    # 다시 연결하면 계정이 바뀌었을 수 있으므로 계정 이메일을 새로 받는다
    conn.account_email = ""
    db.commit()
    try:
        _account_email(db, conn)
    except httpx.HTTPError:
        pass  # 이메일은 안내용이다. 지금 못 받으면 문서를 지정할 때 다시 받는다
    return RedirectResponse(f"{target}?google=connected")


def _access_token(db: Session, conn: GoogleConnection, force: bool = False) -> str:
    if not force and conn.token_expiry and utcnow() < conn.token_expiry and conn.access_token:
        return conn.access_token
    if not conn.refresh_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "Google 연결이 만료되었다. 다시 연결한다")
    r = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": conn.refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise HTTPException(status.HTTP_409_CONFLICT, "Google 토큰 갱신에 실패했다. 다시 연결한다")
    tok = r.json()
    conn.access_token = tok["access_token"]
    conn.token_expiry = utcnow() + timedelta(seconds=int(tok.get("expires_in", 3600)) - 60)
    db.commit()
    return conn.access_token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _drive_get(db: Session, conn: GoogleConnection, url: str, params: dict[str, str]) -> httpx.Response:
    """Drive API를 부른다. 저장해 둔 액세스 토큰이 거부되면 한 번 갱신해서 다시 부른다."""
    r = httpx.get(url, params=params, headers=_bearer(_access_token(db, conn)), timeout=30)
    if r.status_code == 401 and conn.refresh_token:
        r = httpx.get(url, params=params, headers=_bearer(_access_token(db, conn, force=True)), timeout=30)
    return r


def _google_error(r: httpx.Response) -> tuple[str, str]:
    """Google API 오류 응답에서 사유 코드와 설명을 뽑는다."""
    try:
        err = r.json().get("error") or {}
    except ValueError:
        return "", r.text[:200]
    if not isinstance(err, dict):
        return str(err), ""
    reasons = [str(x.get("reason", "")) for x in err.get("errors", []) if isinstance(x, dict)]
    reason = next((x for x in reasons if x), "") or str(err.get("status", ""))
    return reason, str(err.get("message", ""))


def _account_email(db: Session, conn: GoogleConnection) -> str:
    """연결한 Google 계정의 이메일. 처음 한 번 받아 연결에 저장해 두고 화면과 오류 안내에 쓴다."""
    if conn.account_email:
        return conn.account_email
    r = _drive_get(db, conn, DRIVE_ABOUT, {"fields": "user(emailAddress)"})
    if r.status_code < 400:
        conn.account_email = str((r.json().get("user") or {}).get("emailAddress", ""))[:255]
        db.commit()
    return conn.account_email


def _doc_read_error(db: Session, conn: GoogleConnection, r: httpx.Response) -> HTTPException:
    """문서 정보를 받지 못한 이유를 Google이 준 상태와 사유로 나누어 안내한다."""
    reason, message = _google_error(r)
    if r.status_code == 401:
        return HTTPException(status.HTTP_409_CONFLICT, "Google 연결이 만료되었다. Google 계정을 다시 연결한다")
    if r.status_code == 404:
        email = _account_email(db, conn)
        who = f"연결한 Google 계정 {email}" if email else "연결한 Google 계정"
        return HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"{who}에서 이 문서를 찾을 수 없다. 문서를 만든 계정과 연결한 계정이 같은지, 붙여 넣은 주소가 문서 편집 화면의 주소인지 확인한다. "
            "다른 계정의 문서라면 그 계정에서 이 계정을 편집자로 공유한다",
        )
    if r.status_code == 403 and reason in SCOPE_REASONS:
        return HTTPException(
            status.HTTP_409_CONFLICT,
            "Google 계정을 연결할 때 Drive 읽기 권한에 동의하지 않았다. 연결을 끊고 다시 연결하면서 권한을 허용한다",
        )
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        f"Google Drive API가 문서 읽기를 거부했다. 상태 {r.status_code}, 사유 {reason or '알 수 없음'}. {message}".strip(),
    )


def _connection(db: Session, e: Enrollment) -> GoogleConnection:
    conn = db.query(GoogleConnection).filter(GoogleConnection.enrollment_id == e.id).first()
    if conn is None or not conn.refresh_token and not conn.access_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "먼저 Google 계정을 연결한다")
    return conn


@router.post("/api/enrollments/{enrollment_id}/google/doc", response_model=SyncOut)
def set_doc(body: DocIn, e: MyEnrollment, db: DB) -> SyncOut:
    """기록할 문서를 지정한다. 이미 다른 문서를 기록하던 중이어도 새 주소로 바꿀 수 있다."""
    _require_configured()
    require_open_attempt(e)
    conn = _connection(db, e)
    doc_id = parse_doc_id(body.doc_url)
    r = _drive_get(
        db,
        conn,
        f"{DRIVE_FILES}/{doc_id}",
        {"fields": "id,name,mimeType,capabilities(canReadRevisions)", "supportsAllDrives": "true"},
    )
    if r.status_code >= 400:
        raise _doc_read_error(db, conn, r)
    info = r.json()
    if info.get("mimeType") != DOC_MIME:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Google 문서 형식이 아니다. Word 파일이면 Google 문서로 저장한 뒤 그 문서의 주소를 지정한다",
        )
    if (info.get("capabilities") or {}).get("canReadRevisions") is False:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "이 문서의 수정 이력을 읽을 권한이 없다. 문서의 소유자나 편집자인 계정을 연결한다",
        )
    if conn.doc_id != doc_id:
        conn.doc_id = doc_id
        conn.last_revision_id = ""
    conn.doc_name = str(info.get("name", ""))[:255]
    db.commit()
    added = sync_revisions(db, e, conn)
    db.refresh(e)
    return SyncOut(task=task_state(db, e), added=added, doc_id=doc_id)


@router.post("/api/enrollments/{enrollment_id}/google/sync", response_model=SyncOut)
def sync(e: MyEnrollment, db: DB) -> SyncOut:
    _require_configured()
    require_open_attempt(e)
    conn = _connection(db, e)
    if not conn.doc_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "먼저 문서 주소를 지정한다")
    added = sync_revisions(db, e, conn)
    db.refresh(e)
    return SyncOut(task=task_state(db, e), added=added, doc_id=conn.doc_id)


@router.delete("/api/enrollments/{enrollment_id}/google", response_model=TaskStateOut)
def disconnect(e: MyEnrollment, db: DB) -> TaskStateOut:
    conn = db.query(GoogleConnection).filter(GoogleConnection.enrollment_id == e.id).first()
    if conn:
        db.delete(conn)
        db.commit()
    db.refresh(e)
    return task_state(db, e)


def _export_text(rev: dict[str, Any], token: str) -> str | None:
    """수정 이력 하나의 본문을 텍스트로 받는다. 받지 못하면 None을 돌려준다.

    내보내기 주소는 googleusercontent.com으로 리다이렉트되므로 따라가야 본문이 온다.
    """
    link = (rev.get("exportLinks") or {}).get("text/plain")
    if not link:
        return None
    r = httpx.get(link, headers=_bearer(token), timeout=30, follow_redirects=True)
    if r.status_code >= 400:
        return None
    return r.text.lstrip("﻿").replace("\r\n", "\n")


def sync_revisions(db: Session, e: Enrollment, conn: GoogleConnection) -> int:
    """지금 지정된 문서의 새 수정 이력마다 텍스트를 내보내 버전으로 저장하고 추가한 개수를 돌려준다."""
    attempt = current_attempt(e)
    if attempt is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "진행 중인 시도가 없다")
    revisions: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, str] = {"fields": "nextPageToken,revisions(id,modifiedTime,exportLinks)", "pageSize": "200"}
        if page_token:
            params["pageToken"] = page_token
        r = _drive_get(db, conn, f"{DRIVE_FILES}/{conn.doc_id}/revisions", params)
        if r.status_code >= 400:
            reason, _ = _google_error(r)
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Google Drive API에서 수정 이력을 받지 못했다. 상태 {r.status_code}, 사유 {reason or '알 수 없음'}",
            )
        data = r.json()
        revisions.extend(data.get("revisions", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break

    # 수정 이력 번호는 문서마다 1부터 따로 매겨지므로 문서 ID와 묶어서 이미 받은 것을 가린다.
    # 지난 시도에서 받은 이력은 이번 시도에 다시 넣지 않는다.
    known: dict[str, ArtifactVersion] = {}
    for t in e.task_attempts:
        for v in t.versions:
            meta = v.meta or {}
            if v.source == "google" and meta.get("doc_id") == conn.doc_id and meta.get("revision_id"):
                known[str(meta["revision_id"])] = v

    token = _access_token(db, conn)
    added = 0
    for rev in revisions:
        rid = str(rev.get("id", ""))
        if not rid:
            continue
        old = known.get(rid)
        if old is not None:
            # 전에 본문을 받지 못한 버전은 내용만 채운다
            if not old.text_content and not (old.meta or {}).get("text_exported"):
                text = _export_text(rev, token)
                if text is not None:
                    old.text_content = text or None
                    old.meta = {**(old.meta or {}), "text_exported": True}
            continue
        text = _export_text(rev, token)
        modified = rev.get("modifiedTime", "")
        db.add(
            ArtifactVersion(
                attempt_id=attempt.id,
                version_no=len(attempt.versions) + added + 1,
                source="google",
                kind="text",
                text_content=text or None,
                reason=f"Google 문서 수정 이력 {modified}",
                meta={
                    "revision_id": rid,
                    "modified_time": modified,
                    "doc_id": conn.doc_id,
                    "doc_name": conn.doc_name,
                    "text_exported": text is not None,
                },
            )
        )
        added += 1
        conn.last_revision_id = rid
    conn.last_synced_at = utcnow()
    db.commit()
    db.refresh(attempt)
    return added


def parse_modified(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
