from __future__ import annotations

from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError


class InvalidPdfError(ValueError):
    pass


def validate_pdf(content: bytes) -> None:
    if not content.startswith(b"%PDF-"):
        raise InvalidPdfError("The file does not contain a PDF header.")
    try:
        reader = PdfReader(BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise InvalidPdfError("Password-protected PDFs are not supported.")
        if len(reader.pages) < 1:
            raise InvalidPdfError("The PDF contains no pages.")
    except InvalidPdfError:
        raise
    except (PdfReadError, ValueError, TypeError) as error:
        raise InvalidPdfError(
            "The file is malformed and cannot be opened as a PDF."
        ) from error
