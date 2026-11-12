"""환경 설정. 값은 backend/.env 또는 환경 변수에서 읽는다. 비밀값은 코드에 두지 않는다."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
# 개발용 기본 키. 공개 저장소에 적혀 있으므로 운영에서는 쓸 수 없다
DEV_SECRET_KEY = "dev-only-secret-changeme"
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "학습 도우미"
    # development 또는 production. Docker 이미지는 production으로 돈다
    app_env: Literal["development", "production"] = "development"
    secret_key: str = DEV_SECRET_KEY
    access_token_minutes: int = 60 * 24 * 7
    database_url: str = f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"
    media_dir: Path = DATA_DIR / "media"
    frontend_dist: Path = BASE_DIR.parent / "frontend" / "dist"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # AI 공급자. gemini 또는 anthropic. 사고 수준은 아래 ai_effort_* 값을 그대로 쓴다
    ai_provider: Literal["gemini", "anthropic"] = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"
    ai_timeout_seconds: int = 60
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"
    ai_effort_chat: str = "medium"
    ai_effort_judge: str = "low"
    ai_effort_grade: str = "high"
    ai_max_context_chars: int = 12000

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/google/callback"
    # 소셜 로그인. Google 은 위의 GOOGLE_CLIENT_ID 와 GOOGLE_CLIENT_SECRET 을 같이 쓴다
    github_client_id: str = ""
    github_client_secret: str = ""
    # 소셜 로그인 공급자가 돌아올 서버 주소. 공급자 콘솔에 등록한 주소와 같아야 한다
    oauth_redirect_base: str = "http://localhost:8000"
    # 로그인 뒤 돌아갈 프런트엔드 주소. 비우면 같은 서버의 상대 경로로 돌아간다. 개발 중에는 http://localhost:5173
    public_url: str = ""
    downloads_dir: Path = BASE_DIR / "downloads"

    max_material_mb: int = 20
    max_version_mb: int = 30
    companion_snapshot_seconds: int = 60

    @field_validator("database_url")
    @classmethod
    def _sqlite_relative_to_backend(cls, v: str) -> str:
        """sqlite:///./data/app.db 같은 상대 경로는 실행 위치가 아니라 backend 폴더 기준으로 푼다."""
        prefix = "sqlite:///"
        if not v.startswith(prefix):
            return v
        raw = v[len(prefix) :]
        if raw in ("", ":memory:") or raw.startswith("/") or Path(raw).is_absolute():
            return v
        return prefix + (BASE_DIR / raw).resolve().as_posix()

    @model_validator(mode="after")
    def _real_secret_in_production(self) -> Settings:
        """기본 키나 짧은 키로 운영하면 누구나 로그인 토큰을 만들 수 있으므로 운영 환경에서는 서버 시작을 막는다."""
        if self.app_env != "production":
            return self
        weak = self.secret_key == DEV_SECRET_KEY or self.secret_key.startswith("여기에") or len(self.secret_key) < MIN_SECRET_LENGTH
        if weak:
            raise ValueError(f"운영 환경에서는 SECRET_KEY에 {MIN_SECRET_LENGTH}자 이상의 무작위 문자열을 넣어야 한다")
        return self

    @property
    def ai_model(self) -> str:
        return self.gemini_model if self.ai_provider == "gemini" else self.anthropic_model

    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
