from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timezone


def utcnow() -> datetime:
    """DB에는 시간대 없는 UTC로 저장한다."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc_naive(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def new_join_code(length: int = 6) -> str:
    alphabet = string.ascii_uppercase + string.digits
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("1", "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


_WS = re.compile(r"\s+")


def normalize_answer(text: str) -> str:
    """단답형과 완성형 정답 비교용. 공백과 대소문자와 끝 문장부호를 무시한다."""
    t = _WS.sub("", text or "").strip().lower()
    return t.rstrip(".。!?")


def answer_matches(expected: str, given: str) -> bool:
    """정답은 | 로 구분한 대안을 허용한다."""
    g = normalize_answer(given)
    if not g:
        return False
    return any(normalize_answer(alt) == g for alt in (expected or "").split("|") if alt.strip())


def clamp_score(value: float | int | None) -> float:
    if value is None:
        return 0.0
    return float(max(0, min(100, value)))
