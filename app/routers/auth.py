from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from ..deps import DB, CurrentUser
from ..models import User
from ..schemas import LoginIn, SignupIn, TokenOut, UserOut
from ..security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/signup", response_model=TokenOut, status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, db: DB) -> TokenOut:
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입된 이메일이다")
    user = User(
        email=body.email, password_hash=hash_password(body.password), name=body.name.strip(), role=body.role
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return TokenOut(token=create_access_token(user.id, user.role), user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, db: DB) -> TokenOut:
    user = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "이메일 또는 비밀번호가 맞지 않다")
    return TokenOut(token=create_access_token(user.id, user.role), user=UserOut.model_validate(user))


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
