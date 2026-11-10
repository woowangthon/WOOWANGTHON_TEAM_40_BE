"""환경 설정. 값은 backend/.env 또는 환경 변수에서 읽는다. 비밀값은 코드에 두지 않는다."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = "학습 도우미"
    secret_key: str = "dev-only-secret-change-me"
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

    @property
    def ai_model(self) -> str:
        return self.gemini_model if self.ai_provider == "gemini" else self.anthropic_model

    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
