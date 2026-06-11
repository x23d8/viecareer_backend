"""File upload endpoint — extract text from PDF, DOCX, or TXT."""

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from core.clerk_auth import ClerkUser, require_auth
from core.rate_limit import check_user_limit_upload, limiter
from services.file_service import extract_text

router = APIRouter(tags=["files"])

# Payload size is enforced globally by PayloadSizeLimitMiddleware (5 MB for this path).
# IP + user rate limits are enforced here.


@router.post("/parse-file")
@limiter.limit("10/minute")
async def parse_file(
    request: Request,
    file: UploadFile = File(...),
    current_user: ClerkUser = Depends(require_auth),
):
    """Upload a PDF, DOCX, or TXT file and return its extracted text."""
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    await check_user_limit_upload(current_user.user_id)

    file_bytes = await file.read()
    text = extract_text(file.filename, file_bytes)
    return {"text": text}
