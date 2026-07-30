"""File upload endpoint — extract text from PDF, DOCX, or TXT."""

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from core.api_usage import track_user_activity_async
from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import check_user_limit_upload, limiter
from services.file_service import extract_text, save_uploaded_file

router = APIRouter(tags=["files"])

# Payload size is enforced globally by PayloadSizeLimitMiddleware (5 MB for this path).
# IP + user rate limits are enforced here.


@router.post("/parse-file")
@limiter.limit("10/minute")
async def parse_file(
    request: Request,
    file: UploadFile = File(...),
    purpose: Literal["cv", "jd", "other"] = Form("cv"),
    current_user: ClerkUser = Depends(require_auth),
):
    """Parse and persist a PDF, DOCX, or TXT file for cross-device sync."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    await check_user_limit_upload(current_user.user_id)

    started_at = time.perf_counter()
    file_bytes = await file.read()
    text = extract_text(file.filename, file_bytes)
    saved_file = await asyncio.to_thread(
        save_uploaded_file,
        external_auth_id=current_user.user_id,
        filename=file.filename,
        mime_type=file.content_type,
        file_bytes=file_bytes,
        extracted_text=text,
        purpose=purpose,
    )
    track_user_activity_async(
        "file_parse",
        external_auth_id=current_user.user_id,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        meta={
            "file_name": file.filename,
            "content_type": file.content_type,
            "file_size_bytes": len(file_bytes),
            "extracted_chars": len(text or ""),
            "purpose": purpose,
            "uploaded_file_id": saved_file["id"],
        },
    )
    return {"text": text, "file": saved_file}
