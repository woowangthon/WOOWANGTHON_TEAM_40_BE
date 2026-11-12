from __future__ import annotations

from datetime import timedelta
from typing import Any

import bcrypt
import jwt

from .config import settings
from .util import utcnow

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int, role: str) -> str:
    expire = utcnow() + timedelta(minutes=settings.access_token_minutes)
    payload: dict[str, Any] = {"sub": str(user_id), "role": role, "typ": "access", "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    """로그인 토큰만 받는다. 소셜 로그인의 state 와 표처럼 같은 키로 서명한 다른 용도의 토큰은 거절한다.

    typ 이 없는 토큰은 이 검사를 넣기 전에 발급한 로그인 토큰이라 받는다.
    """
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    return data if data.get("typ", "access") == "access" else None
