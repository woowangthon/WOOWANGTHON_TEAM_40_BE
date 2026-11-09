"""자리 2 Google 문서 연동.

학생이 Google 계정을 연결하고 문서 주소를 지정하면 서버가 Google Drive API의 revisions 목록을 받아
새 수정 이력마다 텍스트를 내보내 결과물 버전으로 저장한다. 학습 AI는 같은 사이트의 채팅을 문서 탭 위에 띄운다.
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
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
DOC_ID_RE = re.compile(r"/document/d/([A-Za-z0-9_-]+)")


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
    m = DOC_ID_RE.search(url.strip())
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", url.strip()):
        return url.strip()
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
    db.commit()
    return RedirectResponse(f"{target}?google=connected")


def _access_token(db: Session, conn: GoogleConnection) -> str:
    if conn.token_expiry and utcnow() < conn.token_expiry and conn.access_token:
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


def _connection(db: Session, e: Enrollment) -> GoogleConnection:
    conn = db.query(GoogleConnection).filter(GoogleConnection.enrollment_id == e.id).first()
    if conn is None or not conn.refresh_token and not conn.access_token:
        raise HTTPException(status.HTTP_409_CONFLICT, "먼저 Google 계정을 연결한다")
    return conn


@router.post("/api/enrollments/{enrollment_id}/google/doc", response_model=SyncOut)
def set_doc(body: DocIn, e: MyEnrollment, db: DB) -> SyncOut:
    _require_configured()
    require_open_attempt(e)
    conn = _connection(db, e)
    doc_id = parse_doc_id(body.doc_url)
    token = _access_token(db, conn)
    r = httpx.get(
        f"{DRIVE_FILES}/{doc_id}",
        params={"fields": "id,name,mimeType"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if r.status_code >= 400:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "그 문서를 읽을 수 없다. 연결한 계정이 문서에 접근할 수 있는지 확인한다")
    info = r.json()
    if info.get("mimeType") != "application/vnd.google-apps.document":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Google 문서 형식이 아니다")
    if conn.doc_id != doc_id:
        conn.doc_id = doc_id
        conn.last_revision_id = ""
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


def sync_revisions(db: Session, e: Enrollment, conn: GoogleConnection) -> int:
    """새 수정 이력마다 텍스트를 내보내 버전으로 저장하고 추가한 개수를 돌려준다."""
    attempt = current_attempt(e)
    if attempt is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "진행 중인 시도가 없다")
    token = _access_token(db, conn)
    headers = {"Authorization": f"Bearer {token}"}
    revisions: list[dict[str, Any]] = []
    page_token = ""
    while True:
        params: dict[str, str] = {"fields": "nextPageToken,revisions(id,modifiedTime,exportLinks)", "pageSize": "200"}
        if page_token:
            params["pageToken"] = page_token
        r = httpx.get(f"{DRIVE_FILES}/{conn.doc_id}/revisions", params=params, headers=headers, timeout=30)
        if r.status_code >= 400:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Google Drive API에서 수정 이력을 받지 못했다")
        data = r.json()
        revisions.extend(data.get("revisions", []))
        page_token = data.get("nextPageToken", "")
        if not page_token:
            break

    seen = {v.meta.get("revision_id") for v in attempt.versions if v.source == "google"}
    added = 0
    for rev in revisions:
        rid = str(rev.get("id", ""))
        if not rid or rid in seen:
            continue
        links = rev.get("exportLinks") or {}
        link = links.get("text/plain")
        text = ""
        if link:
            tr = httpx.get(link, headers=headers, timeout=30)
            if tr.status_code < 400:
                text = tr.text
        modified = rev.get("modifiedTime", "")
        db.add(
            ArtifactVersion(
                attempt_id=attempt.id,
                version_no=len(attempt.versions) + added + 1,
                source="google",
                kind="text",
                text_content=text or None,
                reason=f"Google 문서 수정 이력 {modified}",
                meta={"revision_id": rid, "modified_time": modified, "doc_id": conn.doc_id},
            )
        )
        added += 1
        conn.last_revision_id = rid
    db.commit()
    db.refresh(attempt)
    return added


def parse_modified(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
