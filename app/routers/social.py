"""Google 과 GitHub 계정으로 로그인한다. OAuth 2.0 인가 코드 방식이다.

흐름
1. 프런트엔드가 start 로 인가 주소를 받아 그 주소로 이동한다.
2. 공급자가 callback 으로 code 를 돌려준다. 서버가 code 를 토큰으로 바꾸고 프로필에서 확인된 이메일과 이름을 읽는다.
3. 그 이메일의 사용자를 찾고, 없으면 회원가입 화면에서 고른 역할로 만든다. 비밀번호로 가입한 계정에는 들어가지 않는다.
4. 60초 동안 한 번만 쓸 수 있는 표를 붙여 프런트엔드의 /login/social 로 돌려보낸다.
5. 프런트엔드가 exchange 로 표를 로그인 토큰으로 바꾼다. 로그인 토큰을 주소에 싣지 않기 위한 단계다.

state 는 start 를 부른 브라우저에 쿠키로 묶는다. 다른 사람이 만든 callback 주소를 열어도 그 사람 계정으로 로그인되지 않는다.
클라이언트 ID 와 비밀 값은 환경 변수에서만 읽는다. 값이 없는 공급자는 목록에 나오지 않고 start 는 503 을 돌려준다.
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from ..config import settings
from ..deps import DB
from ..models import User
from ..schemas import TokenOut, UserOut
from ..security import ALGORITHM, create_access_token
from ..util import utcnow

router = APIRouter(prefix="/api/auth/social", tags=["auth"])

Provider = Literal["google", "github"]
PROVIDERS: tuple[Provider, ...] = ("google", "github")
STATE_MINUTES = 10
TICKET_SECONDS = 60
HTTP_TIMEOUT = 15
STATE_COOKIE = "social_state"
COOKIE_PATH = "/api/auth/social"

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
GITHUB_AUTH = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"
GITHUB_USER = "https://api.github.com/user"
GITHUB_EMAILS = "https://api.github.com/user/emails"

# 한 번 쓴 표의 번호와 만료 시각. 같은 표로 두 번 로그인하지 못하게 한다. 서버 한 대로 도는 구성을 전제로 한다.
_used_tickets: dict[str, float] = {}
_ticket_lock = threading.Lock()


class StartIn(BaseModel):
    mode: Literal["login", "signup"] = "login"
    role: Literal["professor", "student"] | None = None


class ExchangeIn(BaseModel):
    ticket: str


def _credentials(provider: Provider) -> tuple[str, str]:
    if provider == "google":
        return settings.google_client_id, settings.google_client_secret
    return settings.github_client_id, settings.github_client_secret


def configured(provider: Provider) -> bool:
    client_id, client_secret = _credentials(provider)
    return bool(client_id and client_secret)


def enabled() -> list[str]:
    return [p for p in PROVIDERS if configured(p)]


def _redirect_uri(provider: Provider) -> str:
    return f"{settings.oauth_redirect_base.rstrip('/')}/api/auth/social/{provider}/callback"


def _front(path: str) -> str:
    """프런트엔드 주소. PUBLIC_URL 이 비어 있으면 같은 서버의 상대 경로로 돌아간다."""
    return f"{settings.public_url.rstrip('/')}{path}" if settings.public_url else path


def _encode(payload: dict[str, Any], lifetime: timedelta) -> str:
    return jwt.encode({**payload, "exp": utcnow() + lifetime}, settings.secret_key, algorithm=ALGORITHM)


def _decode(token: str, typ: str) -> dict[str, Any] | None:
    try:
        data = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None
    return data if data.get("typ") == typ else None


def _post_form(url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
    res = httpx.post(url, data=data, headers=headers, timeout=HTTP_TIMEOUT)
    res.raise_for_status()
    return res.json()


def _get_json(url: str, access_token: str) -> Any:
    res = httpx.get(
        url, headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"}, timeout=HTTP_TIMEOUT
    )
    res.raise_for_status()
    return res.json()


def _profile(provider: Provider, code: str) -> tuple[str, str] | None:
    """인가 코드를 프로필로 바꾼다. 확인된 이메일이 없으면 None 이다."""
    client_id, client_secret = _credentials(provider)
    form = {"code": code, "client_id": client_id, "client_secret": client_secret, "redirect_uri": _redirect_uri(provider)}
    if provider == "google":
        token = _post_form(GOOGLE_TOKEN, {**form, "grant_type": "authorization_code"})
        info = _get_json(GOOGLE_USERINFO, str(token["access_token"]))
        if not info.get("email") or not info.get("email_verified"):
            return None
        return str(info["email"]).strip().lower(), str(info.get("name") or "")
    token = _post_form(GITHUB_TOKEN, form, headers={"Accept": "application/json"})
    access = str(token["access_token"])
    user = _get_json(GITHUB_USER, access)
    emails = _get_json(GITHUB_EMAILS, access)
    verified = [e for e in emails if e.get("verified") and e.get("email")]
    primary = next((e for e in verified if e.get("primary")), verified[0] if verified else None)
    if primary is None:
        return None
    return str(primary["email"]).strip().lower(), str(user.get("name") or user.get("login") or "")


@router.get("/providers")
def providers() -> dict[str, list[str]]:
    """키가 설정된 공급자만 돌려준다. 프런트엔드는 여기에 있는 공급자만 버튼으로 보여 준다."""
    return {"providers": enabled()}


@router.post("/exchange", response_model=TokenOut)
def exchange(body: ExchangeIn, db: DB) -> TokenOut:
    data = _decode(body.ticket, "social_ticket")
    if data is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인 요청이 만료되었다. 다시 시도한다")
    now = time.time()
    jti = str(data.get("jti", ""))
    # 확인과 기록을 한 번에 해서 같은 표가 동시에 두 번 통과하지 못하게 한다
    with _ticket_lock:
        for key in [k for k, until in _used_tickets.items() if until < now]:
            del _used_tickets[key]
        used = not jti or jti in _used_tickets
        if not used:
            _used_tickets[jti] = now + TICKET_SECONDS
    if used:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "이미 쓴 로그인 요청이다. 다시 시도한다")
    user = db.get(User, int(data["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없다")
    return TokenOut(token=create_access_token(user.id, user.role), user=UserOut.model_validate(user))


@router.post("/{provider}/start")
def start(provider: Provider, body: StartIn, response: Response) -> dict[str, str]:
    if not configured(provider):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "이 소셜 로그인은 서버에 설정되어 있지 않다")
    if body.mode == "signup" and body.role is None:
        raise HTTPException(422, "가입할 역할을 고른다")
    nonce = secrets.token_urlsafe(16)
    state = _encode(
        {"typ": "social_state", "p": provider, "mode": body.mode, "role": body.role, "n": nonce},
        timedelta(minutes=STATE_MINUTES),
    )
    # state 안의 값과 같은 값을 이 브라우저의 쿠키에 둔다. callback 은 둘이 같을 때만 받는다
    response.set_cookie(
        STATE_COOKIE,
        nonce,
        max_age=STATE_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=settings.oauth_redirect_base.startswith("https://"),
        path=COOKIE_PATH,
    )
    client_id, _ = _credentials(provider)
    params = {"client_id": client_id, "redirect_uri": _redirect_uri(provider), "state": state}
    if provider == "google":
        params.update({"response_type": "code", "scope": "openid email profile", "prompt": "select_account"})
        return {"url": f"{GOOGLE_AUTH}?{urlencode(params)}"}
    params.update({"scope": "read:user user:email"})
    return {"url": f"{GITHUB_AUTH}?{urlencode(params)}"}


@router.get("/{provider}/callback", include_in_schema=False)
def callback(
    provider: Provider, request: Request, db: DB, code: str = "", state: str = "", error: str = ""
) -> RedirectResponse:
    def back(query: str) -> RedirectResponse:
        res = RedirectResponse(_front(f"/login/social?{query}"), status_code=status.HTTP_302_FOUND)
        res.delete_cookie(STATE_COOKIE, path=COOKIE_PATH)
        return res

    if error:
        return back("error=denied")
    data = _decode(state, "social_state")
    if data is None or data.get("p") != provider or not code:
        return back("error=state")
    nonce = str(data.get("n", ""))
    if not nonce or not secrets.compare_digest(request.cookies.get(STATE_COOKIE, ""), nonce):
        return back("error=state")
    if not configured(provider):
        return back("error=provider")
    try:
        profile = _profile(provider, code)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return back("error=provider")
    if profile is None:
        return back("error=email")
    email, name = profile
    user = db.query(User).filter(User.email == email).first()
    if user is not None and user.password_hash:
        # 비밀번호 가입은 이메일 주인인지 확인하지 않는다. 남이 먼저 만들어 둔 계정으로 들어가지 않도록 거절한다
        return back("error=password_account")
    if user is None:
        role = data.get("role")
        if data.get("mode") != "signup" or role not in ("professor", "student"):
            return back("error=no_account")
        # 소셜 계정으로만 가입한 사용자는 비밀번호가 없다. 빈 해시는 어떤 비밀번호와도 맞지 않는다.
        user = User(email=email, password_hash="", name=(name.strip() or email.split("@")[0])[:100], role=role)
        db.add(user)
        db.commit()
        db.refresh(user)
    ticket = _encode(
        {"typ": "social_ticket", "sub": str(user.id), "jti": secrets.token_urlsafe(12)}, timedelta(seconds=TICKET_SECONDS)
    )
    return back(urlencode({"ticket": ticket}))
