"""File parsing — extract plain text from PDF, DOCX, and TXT uploads."""

import io
import logging

from fastapi import HTTPException

logger = logging.getLogger("viecareer")


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
