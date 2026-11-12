"""소셜 로그인. 공급자 서버 호출은 가짜로 바꾸고 인가 코드에서 로그인 토큰까지의 흐름을 확인한다."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.routers import social


class FakeProvider:
    """공급자의 토큰 주소와 프로필 주소를 흉내 낸다."""

    def __init__(self) -> None:
        self.google_info: dict[str, Any] = {"email": "Kim@School.ac.kr", "email_verified": True, "name": "김교수"}
        self.github_user: dict[str, Any] = {"login": "hong", "name": "홍길동"}
        self.github_emails: list[dict[str, Any]] = [
            {"email": "old@test.com", "primary": False, "verified": True},
            {"email": "hong@test.com", "primary": True, "verified": True},
        ]
        self.posted: list[tuple[str, dict[str, str]]] = []

    def post_form(self, url: str, data: dict[str, str], headers: dict[str, str] | None = None) -> dict[str, Any]:
        self.posted.append((url, data))
        return {"access_token": "dummy-provider-access-token"}

    def get_json(self, url: str, access_token: str) -> Any:
        assert access_token == "dummy-provider-access-token"
        if url == social.GOOGLE_USERINFO:
            return self.google_info
        if url == social.GITHUB_USER:
            return self.github_user
        if url == social.GITHUB_EMAILS:
            return self.github_emails
        raise AssertionError(f"예상하지 못한 주소 {url}")


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    fake = FakeProvider()
    monkeypatch.setattr(social, "_post_form", fake.post_form)
    monkeypatch.setattr(social, "_get_json", fake.get_json)
    monkeypatch.setattr(settings, "google_client_id", "google-id")
    monkeypatch.setattr(settings, "google_client_secret", "google-secret")
    monkeypatch.setattr(settings, "github_client_id", "github-id")
    monkeypatch.setattr(settings, "github_client_secret", "github-secret")
    monkeypatch.setattr(settings, "public_url", "")
    social._used_tickets.clear()
    return fake


def _state(client: TestClient, name: str, body: dict[str, str]) -> str:
    res = client.post(f"/api/auth/social/{name}/start", json=body)
    assert res.status_code == 200, res.text
    return parse_qs(urlparse(res.json()["url"]).query)["state"][0]


def _callback(client: TestClient, name: str, state: str, code: str = "code-1") -> dict[str, list[str]]:
    res = client.get(f"/api/auth/social/{name}/callback", params={"code": code, "state": state}, follow_redirects=False)
    assert res.status_code == 302
    location = urlparse(res.headers["location"])
    assert location.path == "/login/social"
    return parse_qs(location.query)


def test_providers_hidden_without_keys(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("google_client_id", "google_client_secret", "github_client_id", "github_client_secret"):
        monkeypatch.setattr(settings, key, "")
    assert client.get("/api/auth/social/providers").json() == {"providers": []}
    res = client.post("/api/auth/social/google/start", json={"mode": "login"})
    assert res.status_code == 503


def test_start_builds_authorize_url(client: TestClient, provider: FakeProvider) -> None:
    assert client.get("/api/auth/social/providers").json() == {"providers": ["google", "github"]}
    res = client.post("/api/auth/social/google/start", json={"mode": "login"})
    url = urlparse(res.json()["url"])
    query = parse_qs(url.query)
    assert url.netloc == "accounts.google.com"
    assert query["client_id"] == ["google-id"]
    assert query["redirect_uri"] == ["http://localhost:8000/api/auth/social/google/callback"]
    assert "google-secret" not in res.text
    # 가입은 역할을 골라야 시작할 수 있다
    assert client.post("/api/auth/social/github/start", json={"mode": "signup"}).status_code == 422


def test_google_signup_then_login(client: TestClient, provider: FakeProvider) -> None:
    state = _state(client, "google", {"mode": "signup", "role": "professor"})
    query = _callback(client, "google", state)
    ticket = query["ticket"][0]
    assert provider.posted[0][1]["client_secret"] == "google-secret"

    res = client.post("/api/auth/social/exchange", json={"ticket": ticket})
    assert res.status_code == 200, res.text
    body = res.json()
    # 이메일은 소문자로 맞추고 역할은 가입 화면에서 고른 것을 쓴다
    assert body["user"] == {"id": body["user"]["id"], "email": "kim@school.ac.kr", "name": "김교수", "role": "professor"}
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200

    # 같은 표는 한 번만 쓸 수 있다
    assert client.post("/api/auth/social/exchange", json={"ticket": ticket}).status_code == 401

    # 다시 로그인하면 같은 계정으로 들어간다
    again = _callback(client, "google", _state(client, "google", {"mode": "login"}))
    second = client.post("/api/auth/social/exchange", json={"ticket": again["ticket"][0]}).json()
    assert second["user"]["id"] == body["user"]["id"]

    # 소셜 계정으로만 가입한 사용자는 비밀번호 로그인이 되지 않는다
    res = client.post("/api/auth/login", json={"email": "kim@school.ac.kr", "password": "password123"})
    assert res.status_code == 401


def test_login_without_account_is_refused(client: TestClient, provider: FakeProvider) -> None:
    query = _callback(client, "google", _state(client, "google", {"mode": "login"}))
    assert query == {"error": ["no_account"]}


def test_password_account_is_not_entered(client: TestClient, provider: FakeProvider) -> None:
    """비밀번호 가입은 이메일 주인인지 확인하지 않는다. 남이 먼저 만든 계정으로 소셜 로그인이 들어가면 안 된다."""
    res = client.post(
        "/api/auth/signup", json={"email": "hong@test.com", "password": "password123", "name": "홍길동", "role": "student"}
    )
    assert res.status_code in (200, 201), res.text
    # GitHub 은 확인된 기본 이메일 hong@test.com 을 돌려준다
    for body in ({"mode": "login"}, {"mode": "signup", "role": "professor"}):
        assert _callback(client, "github", _state(client, "github", body)) == {"error": ["password_account"]}
    # 비밀번호 로그인은 그대로 된다
    res = client.post("/api/auth/login", json={"email": "hong@test.com", "password": "password123"})
    assert res.status_code == 200


def test_state_is_bound_to_the_browser(client: TestClient, provider: FakeProvider) -> None:
    """start 를 부른 브라우저의 쿠키가 없으면 callback 을 받지 않는다."""
    state = _state(client, "google", {"mode": "signup", "role": "student"})
    client.cookies.clear()
    assert _callback(client, "google", state) == {"error": ["state"]}
    # 다른 브라우저가 새로 시작해 쿠키가 바뀐 뒤에는 앞의 state 를 받지 않는다
    old = _state(client, "google", {"mode": "signup", "role": "student"})
    _state(client, "google", {"mode": "signup", "role": "student"})
    assert _callback(client, "google", old) == {"error": ["state"]}
    # 한 번 돌아온 뒤에는 쿠키가 지워져 같은 state 를 다시 쓰지 못한다
    fresh = _state(client, "google", {"mode": "signup", "role": "student"})
    assert "ticket" in _callback(client, "google", fresh)
    assert _callback(client, "google", fresh) == {"error": ["state"]}


def test_state_and_ticket_are_not_login_tokens(client: TestClient, provider: FakeProvider) -> None:
    """주소에 실리는 state 와 표는 로그인 토큰으로 통하지 않는다."""
    state = _state(client, "google", {"mode": "signup", "role": "student"})
    ticket = _callback(client, "google", state)["ticket"][0]
    for token in (state, ticket):
        assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # 표를 로그인 토큰으로 바꾼 뒤에도 표 자체는 통하지 않는다
    token = client.post("/api/auth/social/exchange", json={"ticket": ticket}).json()["token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {ticket}"}).status_code == 401


def test_unverified_email_is_refused(client: TestClient, provider: FakeProvider) -> None:
    provider.google_info["email_verified"] = False
    query = _callback(client, "google", _state(client, "google", {"mode": "signup", "role": "student"}))
    assert query == {"error": ["email"]}
    provider.github_emails = [{"email": "hong@test.com", "primary": True, "verified": False}]
    query = _callback(client, "github", _state(client, "github", {"mode": "signup", "role": "student"}))
    assert query == {"error": ["email"]}


def test_bad_state_and_denial(client: TestClient, provider: FakeProvider) -> None:
    assert _callback(client, "google", "not-a-state") == {"error": ["state"]}
    # 다른 공급자용으로 만든 state 는 받지 않는다
    assert _callback(client, "github", _state(client, "google", {"mode": "login"})) == {"error": ["state"]}
    res = client.get("/api/auth/social/google/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert parse_qs(urlparse(res.headers["location"]).query) == {"error": ["denied"]}
    assert client.post("/api/auth/social/exchange", json={"ticket": "nope"}).status_code == 401
