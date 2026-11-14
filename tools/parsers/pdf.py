import io

import pdfplumber


def extract_text(data: bytes) -> str:
    from tools.parsers import ParseError

    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as exc:
        raise ParseError(f"cannot read PDF: {exc}") from exc
    if not text.strip():
        raise ParseError("PDF contains no extractable text")
    return text
