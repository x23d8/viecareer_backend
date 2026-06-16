"""Shared chat logic — used by all /chat* endpoints with different system prompts."""

import logging
import time
from typing import List, Optional

from fastapi import HTTPException

from config import OPENAI_MODEL
from core.api_usage import UsageContext, track_openai_usage_async
from core.retry import openai_retry
from schemas.chat import ChatMessage

logger = logging.getLogger("viecareer")


@openai_retry
def _raw_chat(client, model, messages, max_tokens):
    return client.chat.completions.create(
        model=model,
        messages=messages,  # type: ignore
        max_tokens=max_tokens,
    )


def run_chat(
    client,
    *,
    system_prompt: str,
    message: str,
    history: Optional[List[ChatMessage]],
    max_tokens: int,
    usage_context: UsageContext | None = None,
) -> Optional[str]:
    """Build the message list, call the model, and return the reply text."""
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": message})

    started_at = time.perf_counter()
    try:
        response = _raw_chat(client, OPENAI_MODEL, messages, max_tokens)
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        track_openai_usage_async(
            None,
            usage_context,
            status="error",
            model=OPENAI_MODEL,
            duration_ms=duration_ms,
            error_code=exc.__class__.__name__,
            meta={"max_tokens": max_tokens, "history_messages": len(history or [])},
        )
        logger.error("OpenAI failed after 3 attempts")
        raise HTTPException(status_code=503, detail="AI service is unavailable")

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    track_openai_usage_async(
        response,
        usage_context,
        model=OPENAI_MODEL,
        duration_ms=duration_ms,
        meta={"max_tokens": max_tokens, "history_messages": len(history or [])},
    )
    return response.choices[0].message.content
