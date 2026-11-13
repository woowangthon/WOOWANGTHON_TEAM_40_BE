from __future__ import annotations

import re
import secrets
import string
from datetime import datetime, timezone
from pathlib import Path


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


def answer_matches(expected: str, given: str, alternatives: bool = True) -> bool:
    """단답형과 완성형 정답은 | 로 구분한 대안을 허용한다.

    선택형은 alternatives=False 로 불러 보기 전체가 정답과 같을 때만 맞는 것으로 본다. 보기에 | 가 들어 있어도 나누지 않는다.
    """
    g = normalize_answer(given)
    if not g:
        return False
    if normalize_answer(expected or "") == g:
        return True
    if not alternatives:
        return False
    return any(normalize_answer(alt) == g for alt in (expected or "").split("|") if alt.strip())


def clamp_score(value: float | int | None) -> float:
    if value is None:
        return 0.0
    return float(max(0, min(100, value)))


def static_file(root: Path, relative: str) -> Path | None:
    """root 폴더 안에 실제로 있는 파일만 돌려준다.

    주소에 .. 이나 절대 경로를 넣어 폴더 밖의 파일을 가리키면 None 을 돌려준다.
    빌드된 화면 파일을 서비스할 때 데이터 폴더 같은 다른 파일이 새어 나가지 않게 한다.
    """
    if not relative:
        return None
    base = root.resolve()
    candidate = (base / relative).resolve()
    if not candidate.is_relative_to(base) or not candidate.is_file():
        return None
    return candidate
