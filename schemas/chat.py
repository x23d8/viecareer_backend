"""Request schemas for the chat endpoints."""

from typing import List, Optional

from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str  # "user" or "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: Optional[List[ChatMessage]] = None
    user_id: Optional[str] = None  # Clerk user_id; used for user-level rate limiting
