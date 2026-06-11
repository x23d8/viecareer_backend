"""Loader for system prompts stored as plain-text files under backend/prompts/."""

from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache
def load_prompt(name: str) -> str:
    """Read prompts/<name>.txt and return its contents. Cached after first read."""
    return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()
