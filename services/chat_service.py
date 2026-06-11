"""Shared chat logic — used by all /chat* endpoints with different system prompts."""

import logging
from typing import List, Optional

from fastapi import HTTPException

from config import OPENAI_MODEL
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
) -> Optional[str]:
    """Build the message list, call the model, and return the reply text."""
    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend({"role": m.role, "content": m.content} for m in history)
    messages.append({"role": "user", "content": message})

    try:
        response = _raw_chat(client, OPENAI_MODEL, messages, max_tokens)
    except Exception:
        logger.error("OpenAI failed after 3 attempts")
        raise HTTPException(status_code=503, detail="AI service is unavailable")

    return response.choices[0].message.content
