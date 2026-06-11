"""Shared helpers for low-temperature JSON-producing LLM calls."""

import logging
from typing import Optional

from fastapi import HTTPException

from config import OPENAI_MODEL
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
) -> Optional[str]:
    """Run a low-temperature completion expected to return a JSON string."""
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
    except Exception:
        logger.error("OpenAI failed after 3 attempts")
        raise HTTPException(status_code=503, detail="AI service is unavailable")

    return response.choices[0].message.content

