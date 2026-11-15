"""Document parsers: bytes -> ExtractedInvoice (structured formats) or text (PDF/TXT)."""

import csv
import io
from pathlib import Path

from models import DocFormat, ExtractedInvoice


class ParseError(Exception):
    """The document could not be read as the format its suffix claims."""


_SUFFIXES: dict[str, DocFormat] = {
    ".json": "json",
    ".csv": "csv",
    ".xml": "xml",
    ".txt": "txt",
    ".pdf": "pdf",
}


def detect_format(path: Path) -> DocFormat:
    fmt = _SUFFIXES.get(Path(path).suffix.lower())
    if fmt is None:
        raise ParseError(f"unsupported file type: {Path(path).suffix or Path(path).name!r}")
    return fmt


def decode_text(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def extract_pdf_text(data: bytes) -> str:
    from tools.parsers import pdf

    return pdf.extract_text(data)


def parse_structured(fmt: DocFormat, data: bytes) -> ExtractedInvoice:
    """Parse json/csv/xml bytes. CSV layout (key/value vs table) is auto-detected."""
    from tools.parsers import csv_kv, csv_table, json_, xml_

    text = decode_text(data)
    try:
        if fmt == "json":
            return json_.parse_json(text)
        if fmt == "xml":
            return xml_.parse_xml(text)
        if fmt == "csv":
            first = next(csv.reader(io.StringIO(text)), None)
            cells = [c.strip().lower() for c in first or []]
            if cells[:2] == ["field", "value"]:
                return csv_kv.parse_csv_kv(text)
            return csv_table.parse_csv_table(text)
    except ParseError:
        raise
    except Exception as exc:  # pydantic/csv/etree errors all mean "malformed"
        raise ParseError(f"cannot parse {fmt}: {exc}") from exc
    raise ParseError(f"not a structured format: {fmt}")
