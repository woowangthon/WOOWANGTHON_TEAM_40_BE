from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .models import Assignment, CompanionToken, Enrollment, User
from .security import decode_access_token
from .util import utcnow

bearer = HTTPBearer(auto_error=False)

DB = Annotated[Session, Depends(get_db)]


def get_current_user(
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요하다")
    payload = decode_access_token(creds.credentials)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "토큰이 유효하지 않다")
    user = db.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없다")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_professor(user: CurrentUser) -> User:
    if user.role != "professor":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "교수만 할 수 있다")
    return user


def require_student(user: CurrentUser) -> User:
    if user.role != "student":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "학생만 할 수 있다")
    return user


Professor = Annotated[User, Depends(require_professor)]
Student = Annotated[User, Depends(require_student)]


def get_owned_assignment(assignment_id: int, user: Professor, db: DB) -> Assignment:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None or assignment.professor_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "과제를 찾을 수 없다")
    return assignment


OwnedAssignment = Annotated[Assignment, Depends(get_owned_assignment)]


def get_my_enrollment(enrollment_id: int, user: Student, db: DB) -> Enrollment:
    enrollment = db.get(Enrollment, enrollment_id)
    if enrollment is None or enrollment.student_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "참여 정보를 찾을 수 없다")
    return enrollment


MyEnrollment = Annotated[Enrollment, Depends(get_my_enrollment)]


def get_companion_enrollment(
    db: DB, x_companion_token: Annotated[str | None, Header()] = None
) -> Enrollment:
    if not x_companion_token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "보조 프로그램 토큰이 없다")
    row = db.query(CompanionToken).filter(CompanionToken.token == x_companion_token).first()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "보조 프로그램 토큰이 유효하지 않다")
    row.last_seen = utcnow()
    db.commit()
    return row.enrollment


CompanionEnrollment = Annotated[Enrollment, Depends(get_companion_enrollment)]
