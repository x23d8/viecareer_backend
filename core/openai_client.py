"""OpenAI client provider — usable directly or as a FastAPI dependency."""

from functools import lru_cache

import httpx
from openai import OpenAI

from config import OPENAI_API_KEY


@lru_cache
def get_openai_client() -> OpenAI:
    """Return a shared OpenAI client. Cached so it is created only once."""
    return OpenAI(api_key=OPENAI_API_KEY, timeout=httpx.Timeout(30.0))
