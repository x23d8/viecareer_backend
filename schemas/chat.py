"""Request schemas for the chat endpoints."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=20_000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=20_000)
    history: Optional[List[ChatMessage]] = Field(default=None, max_length=50)