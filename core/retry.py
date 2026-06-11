"""Tenacity-based retry decorator for transient OpenAI API errors."""

import logging

from openai import APIConnectionError, APITimeoutError, RateLimitError
from tenacity import RetryCallState, retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger("viecareer")

_MAX_ATTEMPTS = 3


def _before_sleep(retry_state: RetryCallState) -> None:
    exc = retry_state.outcome.exception()
    logger.warning(
        "OpenAI retry %d/%d — %s",
        retry_state.attempt_number,
        _MAX_ATTEMPTS,
        type(exc).__name__,
    )


# Apply to any function that makes a raw OpenAI API call.
# Retries on transient errors (rate limit, timeout, connection drop).
# Does NOT retry on permanent errors (AuthenticationError, BadRequestError).
openai_retry = retry(
    stop=stop_after_attempt(_MAX_ATTEMPTS),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception_type((RateLimitError, APITimeoutError, APIConnectionError)),
    before_sleep=_before_sleep,
    reraise=True,
)
