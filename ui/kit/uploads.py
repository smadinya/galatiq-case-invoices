"""Uploaded files: content-addressed storage under `data/uploads/` (git-ignored).

The same bytes always map to the same path, so the service short-circuits a repeat; different
bytes with the same name never overwrite each other, because the source panel re-reads a
structured file from its stored path.
"""

import hashlib
import re
from pathlib import Path
from typing import Sequence

ALLOWED_SUFFIXES = (".json", ".csv", ".xml", ".txt", ".pdf")
_BAD_CHARS = re.compile(r"[^A-Za-z0-9._ -]")
_MAX_NAME = 120


def safe_name(name: str) -> str:
    """The basename with every character outside `[A-Za-z0-9._ -]` replaced by `_`; at most 120 characters."""
    base = re.split(r"[\\/]", str(name))[-1].strip()
    base = _BAD_CHARS.sub("_", base)
    if len(base) > _MAX_NAME:
        stem, dot, suffix = base.rpartition(".")
        if dot and len(suffix) <= 8:
            base = stem[: _MAX_NAME - len(suffix) - 1] + "." + suffix
        else:
            base = base[:_MAX_NAME]
    if not base.strip(". "):
        return "upload"
    return base


def _suffix_ok(name: str) -> bool:
    return Path(name).suffix.lower() in ALLOWED_SUFFIXES


def store_upload(upload_dir: Path, name: str, data: bytes) -> Path:
    """`<upload_dir>/<sha256(data)[:12]>/<safe_name>`. Idempotent. ValueError for empty data or a bad suffix."""
    safe = safe_name(name)
    if not data:
        raise ValueError("The file is empty.")
    if not _suffix_ok(safe):
        raise ValueError("Unsupported file type; use PDF, TXT, JSON, CSV or XML.")
    target = Path(upload_dir) / hashlib.sha256(data).hexdigest()[:12] / safe
    if not target.is_file() or target.read_bytes() != data:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return target


def store_batch_upload(upload_dir: Path, files: Sequence[tuple[str, bytes]]) -> Path:
    """`<upload_dir>/batch-<digest12>/`: safe names (`-2` before the suffix on clashes); bad suffixes and empty files skipped."""
    kept = [(safe_name(n), d) for n, d in files if d and _suffix_ok(safe_name(n))]
    if not kept:
        raise ValueError("No supported files in the upload.")
    digest = hashlib.sha256(
        "\n".join(f"{n}:{hashlib.sha256(d).hexdigest()}" for n, d in sorted(kept, key=lambda x: (x[0], hashlib.sha256(x[1]).hexdigest()))).encode()
    ).hexdigest()[:12]
    folder = Path(upload_dir) / f"batch-{digest}"
    folder.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()
    for name, data in kept:
        final = name
        n = 2
        while final.lower() in taken:
            p = Path(name)
            final = f"{p.stem}-{n}{p.suffix}"
            n += 1
        taken.add(final.lower())
        (folder / final).write_bytes(data)
    return folder


def _files_in(folder: Path, *, allowed_only: bool) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(
        p for p in folder.iterdir()
        if p.is_file() and not p.name.startswith(".") and (not allowed_only or p.suffix.lower() in ALLOWED_SUFFIXES)
    )


def sample_files(samples_dir: Path) -> list[Path]:
    """Sorted, non-dot files: the same rule `Service.run_batch` uses."""
    return _files_in(Path(samples_dir), allowed_only=False)


def candidate_folders(data_dir: Path, upload_dir: Path) -> list[Path]:
    """Direct children of `data_dir` holding at least one supported file, then `upload_dir/batch-*`."""
    data_dir, upload_dir = Path(data_dir), Path(upload_dir)
    found: list[Path] = []
    if data_dir.is_dir():
        for child in sorted(data_dir.iterdir()):
            if child.is_dir() and child != upload_dir and _files_in(child, allowed_only=True):
                found.append(child)
    if upload_dir.is_dir():
        found.extend(c for c in sorted(upload_dir.glob("batch-*")) if c.is_dir() and _files_in(c, allowed_only=True))
    return found
