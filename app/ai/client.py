"""Anthropic 공식 SDK 호출 래퍼. 모든 호출은 구조화 출력으로 받고 AICallLog에 기록한다."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AICallLog

T = TypeVar("T", bound=BaseModel)


class AIUnavailable(Exception):
    """API 키가 없거나 호출이 거부되어 AI 기능을 쓸 수 없다."""


class AIClient:
    def __init__(self) -> None:
        self._client: Any = None
        if settings.anthropic_api_key:
            import anthropic

            self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    @property
    def available(self) -> bool:
        return self._client is not None

    def parse(
        self,
        *,
        purpose: str,
        system: str,
        messages: list[dict[str, Any]],
        output_model: type[T],
        effort: str = "medium",
        max_tokens: int = 4000,
        db: Session | None = None,
    ) -> T:
        if self._client is None:
            raise AIUnavailable("ANTHROPIC_API_KEY가 설정되지 않아 AI 기능을 쓸 수 없다")
        log = AICallLog(purpose=purpose, model=settings.anthropic_model)
        try:
            response = self._client.messages.parse(
                model=settings.anthropic_model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_format=output_model,
                output_config={"effort": effort},
            )
        except Exception as exc:  # noqa: BLE001  SDK 예외를 그대로 기록한다
            log.ok = False
            log.error = f"{type(exc).__name__}: {exc}"[:2000]
            self._save(db, log)
            raise AIUnavailable(f"AI 호출 실패: {type(exc).__name__}") from exc

        usage = getattr(response, "usage", None)
        if usage is not None:
            log.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            log.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if getattr(response, "stop_reason", None) == "refusal":
            log.ok = False
            log.error = "refusal"
            self._save(db, log)
            raise AIUnavailable("AI가 이 요청을 거부했다")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            log.ok = False
            log.error = "no parsed output"
            self._save(db, log)
            raise AIUnavailable("AI 응답을 해석하지 못했다")
        self._save(db, log)
        return parsed  # type: ignore[no-any-return]

    @staticmethod
    def _save(db: Session | None, log: AICallLog) -> None:
        if db is None:
            return
        db.add(log)
        db.commit()


ai_client = AIClient()
