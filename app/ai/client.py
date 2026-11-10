"""AI 호출 래퍼. 공급자는 AI_PROVIDER로 고르고 기본은 Gemini다.

모든 호출은 구조화 출력으로 받고 AICallLog에 기록한다. Gemini는 Google 공식 SDK(google-genai),
Claude는 Anthropic 공식 SDK를 쓴다.
"""

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
        self.provider = settings.ai_provider
        self._client: Any = None
        if self.provider == "gemini" and settings.gemini_api_key:
            from google import genai
            from google.genai import types

            # 무료 등급은 혼잡할 때 응답이 몇 분씩 묶이므로 제한 시간을 두고 재시도는 한 번만 한다
            self._client = genai.Client(
                api_key=settings.gemini_api_key,
                http_options=types.HttpOptions(
                    timeout=settings.ai_timeout_seconds * 1000,
                    retry_options=types.HttpRetryOptions(attempts=2),
                ),
            )
        elif self.provider == "anthropic" and settings.anthropic_api_key:
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
            key = "GEMINI_API_KEY" if self.provider == "gemini" else "ANTHROPIC_API_KEY"
            raise AIUnavailable(f"{key}가 설정되지 않아 AI 기능을 쓸 수 없다")
        log = AICallLog(purpose=purpose, model=settings.ai_model)
        try:
            if self.provider == "gemini":
                parsed = self._gemini(log, system, messages, output_model, effort)
            else:
                parsed = self._anthropic(log, system, messages, output_model, effort, max_tokens)
        except AIUnavailable as exc:
            log.ok = False
            log.error = log.error or str(exc)
            self._save(db, log)
            raise
        except Exception as exc:  # noqa: BLE001  SDK 예외를 그대로 기록한다
            log.ok = False
            log.error = f"{type(exc).__name__}: {exc}"[:2000]
            self._save(db, log)
            code = getattr(exc, "code", None)
            if code == 429:
                raise AIUnavailable("AI 무료 사용 한도를 넘었다. 잠시 뒤 다시 시도한다") from exc
            if code in (500, 503, 504) or "Timeout" in type(exc).__name__:
                raise AIUnavailable("AI 서버가 혼잡해 응답하지 못했다. 잠시 뒤 다시 시도한다") from exc
            raise AIUnavailable(f"AI 호출 실패: {type(exc).__name__}") from exc
        self._save(db, log)
        return parsed

    def _gemini(
        self, log: AICallLog, system: str, messages: list[dict[str, Any]], output_model: type[T], effort: str
    ) -> T:
        from google.genai import types

        contents = [
            types.Content(
                role="model" if m["role"] == "assistant" else "user",
                parts=[types.Part.from_text(text=str(m["content"]))],
            )
            for m in messages
        ]
        # max_output_tokens는 사고 토큰까지 함께 세므로 낮게 잡으면 JSON이 잘린다. 그래서 지정하지 않는다.
        response = self._client.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=output_model,
                thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel(effort.upper())),
            ),
        )
        usage = response.usage_metadata
        if usage is not None:
            log.input_tokens = int(usage.prompt_token_count or 0)
            log.output_tokens = int(usage.candidates_token_count or 0) + int(usage.thoughts_token_count or 0)
        parsed = response.parsed
        if isinstance(parsed, output_model):
            return parsed
        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason is not None:
            log.error = f"blocked: {feedback.block_reason}"
            raise AIUnavailable("AI가 이 요청을 거부했다")
        reason = response.candidates[0].finish_reason if response.candidates else None
        log.error = f"no parsed output, finish_reason={reason}"
        raise AIUnavailable("AI 응답을 해석하지 못했다")

    def _anthropic(
        self,
        log: AICallLog,
        system: str,
        messages: list[dict[str, Any]],
        output_model: type[T],
        effort: str,
        max_tokens: int,
    ) -> T:
        response = self._client.messages.parse(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            output_format=output_model,
            output_config={"effort": effort},
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            log.input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            log.output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        if getattr(response, "stop_reason", None) == "refusal":
            log.error = "refusal"
            raise AIUnavailable("AI가 이 요청을 거부했다")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            log.error = "no parsed output"
            raise AIUnavailable("AI 응답을 해석하지 못했다")
        return parsed  # type: ignore[no-any-return]

    @staticmethod
    def _save(db: Session | None, log: AICallLog) -> None:
        if db is None:
            return
        db.add(log)
        db.commit()


ai_client = AIClient()
