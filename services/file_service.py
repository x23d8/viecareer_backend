"""File parsing — extract plain text from PDF, DOCX, and TXT uploads."""

import io
import logging
from hashlib import sha256
from typing import Any

from fastapi import HTTPException, status

from core.database import get_connection

logger = logging.getLogger("viecareer")

MAX_SYNCED_EXTRACTED_TEXT_CHARS = 60_000


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract text from PDF using available pure-Python PDF parsers."""
    parser_errors: list[str] = []

    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except ImportError as exc:
        parser_errors.append(f"pypdf import failed: {exc}")
    except Exception as exc:
        parser_errors.append(f"pypdf parse failed: {exc}")

    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)
    except ImportError as exc:
        parser_errors.append(f"PyPDF2 import failed: {exc}")
    except Exception as exc:
        parser_errors.append(f"PyPDF2 parse failed: {exc}")

    try:
        from pdfminer.high_level import extract_text as pdfminer_extract_text

        return pdfminer_extract_text(io.BytesIO(file_bytes))
    except ImportError as exc:
        parser_errors.append(f"pdfminer import failed: {exc}")
    except Exception as exc:
        parser_errors.append(f"pdfminer parse failed: {exc}")

    logger.warning("PDF parsing failed: %s", "; ".join(parser_errors))
    raise HTTPException(status_code=422, detail="PDF could not be read.")


def extract_text_from_docx(file_bytes: bytes) -> str:
    """Extract text from DOCX using python-docx."""
    try:
        import docx
    except ImportError:
        raise HTTPException(status_code=422, detail="python-docx is not installed.")

    try:
        doc = docx.Document(io.BytesIO(file_bytes))
        return "\n".join(para.text for para in doc.paragraphs if para.text.strip())
    except Exception:
        raise HTTPException(status_code=422, detail="DOCX could not be read.")


def extract_text(filename: str, file_bytes: bytes) -> str:
    """Dispatch on file extension and return the extracted, stripped text."""
    name = filename.lower()

    if name.endswith(".pdf"):
        text = extract_text_from_pdf(file_bytes)
    elif name.endswith(".docx"):
        text = extract_text_from_docx(file_bytes)
    elif name.endswith(".txt"):
        text = file_bytes.decode("utf-8", errors="replace")
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {filename}. Use PDF, DOCX, or TXT.",
        )

    if not text or not text.strip():
        raise HTTPException(
            status_code=422,
            detail="Could not extract any text from the uploaded file.",
        )

    return text.strip()


def save_uploaded_file(
    *,
    external_auth_id: str,
    filename: str,
    mime_type: str | None,
    file_bytes: bytes,
    extracted_text: str,
    purpose: str = "cv",
) -> dict[str, Any]:
    """Persist a parsed upload so it can be hydrated on another client."""
    normalized_purpose = str(purpose or "cv").strip().lower()
    if normalized_purpose not in {"cv", "jd", "other"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid file purpose.")

    file_type = _file_type(filename)
    content_hash = sha256(file_bytes).hexdigest()
    synced_text = str(extracted_text or "")[:MAX_SYNCED_EXTRACTED_TEXT_CHARS]

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                _ensure_uploaded_file_sync_columns(cur)
                cur.execute(
                    """
                    select id
                    from users
                    where external_auth_id = %s
                    limit 1
                    """,
                    (external_auth_id,),
                )
                user = cur.fetchone()
                if not user:
                    raise HTTPException(
                        status.HTTP_404_NOT_FOUND,
                        detail="User profile has not been synced yet.",
                    )

                cur.execute(
                    """
                    insert into uploaded_files (
                      user_id,
                      file_name,
                      file_type,
                      mime_type,
                      file_size_bytes,
                      storage_url,
                      extracted_text,
                      file_purpose,
                      content_sha256
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (user_id, file_purpose, content_sha256)
                    where content_sha256 is not null
                    do update set
                      file_name = excluded.file_name,
                      mime_type = excluded.mime_type,
                      file_size_bytes = excluded.file_size_bytes,
                      extracted_text = excluded.extracted_text,
                      created_at = now()
                    returning
                      id::text as id,
                      file_name,
                      file_type,
                      mime_type,
                      file_size_bytes,
                      storage_url,
                      extracted_text,
                      file_purpose,
                      created_at
                    """,
                    (
                        user["id"],
                        filename,
                        file_type,
                        mime_type or _default_mime_type(file_type),
                        len(file_bytes),
                        "database://uploaded-files/extracted-text",
                        synced_text,
                        normalized_purpose,
                        content_hash,
                    ),
                )
                row = cur.fetchone()
            conn.commit()
        return dict(row)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Could not persist uploaded file for user=%s", external_auth_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not save the uploaded file.",
        ) from exc


def _ensure_uploaded_file_sync_columns(cur) -> None:
    cur.execute(
        """
        alter table uploaded_files
        add column if not exists file_purpose varchar(20) not null default 'cv'
        """
    )
    cur.execute(
        """
        alter table uploaded_files
        add column if not exists content_sha256 varchar(64) null
        """
    )
    cur.execute(
        """
        create unique index if not exists idx_uploaded_files_user_purpose_hash
        on uploaded_files (user_id, file_purpose, content_sha256)
        where content_sha256 is not null
        """
    )


def _file_type(filename: str) -> str:
    lowered = str(filename or "").lower()
    for file_type in ("pdf", "docx", "txt"):
        if lowered.endswith(f".{file_type}"):
            return file_type
    raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unsupported file type.")


def _default_mime_type(file_type: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
    }[file_type]
