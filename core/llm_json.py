"""Shared helpers for low-temperature JSON-producing LLM calls."""

import logging
import time
from typing import Optional

from fastapi import HTTPException

from config import OPENAI_MODEL
from core.api_usage import UsageContext, track_openai_usage_async
from core.retry import openai_retry

logger = logging.getLogger("viecareer")


@openai_retry
def _raw_completion(client, model, messages, max_tokens, temperature):
    return client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )


def json_completion(
    client,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    temperature: float = 0.3,
    usage_context: UsageContext | None = None,
) -> Optional[str]:
    """Run a low-temperature completion expected to return a JSON string."""
    started_at = time.perf_counter()
    try:
        response = _raw_completion(
            client,
            OPENAI_MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens,
            temperature,
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        track_openai_usage_async(
            None,
            usage_context,
            status="error",
            model=OPENAI_MODEL,
            duration_ms=duration_ms,
            error_code=exc.__class__.__name__,
            meta={"max_tokens": max_tokens, "temperature": temperature},
        )
        logger.error("OpenAI failed after 3 attempts")
        raise HTTPException(status_code=503, detail="AI service is unavailable")

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    track_openai_usage_async(
        response,
        usage_context,
        model=OPENAI_MODEL,
        duration_ms=duration_ms,
        meta={"max_tokens": max_tokens, "temperature": temperature},
    )
    return response.choices[0].message.content

