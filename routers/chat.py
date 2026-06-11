"""Chat endpoints — generic assistant, VieBot, and AI interviewer."""

from fastapi import APIRouter, Depends, Request

from core.clerk_auth import ClerkUser, require_auth
from core.openai_client import get_openai_client
from core.prompts import load_prompt
from core.rate_limit import (
    ai_concurrency_limit,
    check_daily_quota,
    check_user_limit_ai,
    limiter,
)
from schemas.chat import ChatRequest
from services.chat_service import run_chat

router = APIRouter(tags=["chat"])


@router.post("/chat")
@limiter.limit("20/minute")
async def chat(
    request: Request,
    req: ChatRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """Generic helpful assistant."""
    await check_daily_quota()
    await check_user_limit_ai(current_user.user_id)
    async with ai_concurrency_limit():
        reply = run_chat(
            client,
            system_prompt="You are a helpful assistant.",
            message=req.message,
            history=req.history,
            max_tokens=300,
        )
    return {"reply": reply}


@router.post("/chat/viebot")
@limiter.limit("20/minute")
async def chat_viebot(
    request: Request,
    req: ChatRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """VieBot — career assistant chatbot."""
    await check_daily_quota()
    await check_user_limit_ai(current_user.user_id)
    async with ai_concurrency_limit():
        reply = run_chat(
            client,
            system_prompt=load_prompt("viebot"),
            message=req.message,
            history=req.history,
            max_tokens=500,
        )
    return {"reply": reply}


@router.post("/chat/interview")
@limiter.limit("15/minute")
async def chat_interview(
    request: Request,
    req: ChatRequest,
    current_user: ClerkUser = Depends(require_auth),
    client=Depends(get_openai_client),
):
    """AI Interviewer — technical interview conductor."""
    await check_daily_quota()
    await check_user_limit_ai(current_user.user_id)
    async with ai_concurrency_limit():
        reply = run_chat(
            client,
            system_prompt=load_prompt("interviewer"),
            message=req.message,
            history=req.history,
            max_tokens=500,
        )
    return {"reply": reply}
