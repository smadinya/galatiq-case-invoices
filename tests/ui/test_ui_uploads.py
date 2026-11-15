import hashlib

import pytest

from ui.kit.uploads import (
    candidate_folders, safe_name, sample_files, store_batch_upload, store_upload,
)


def test_safe_name_strips_paths_and_odd_characters():
    assert safe_name("../../x.json") == "x.json"
    assert safe_name("C:\\temp\\y.csv") == "y.csv"
    assert safe_name("rechnung é€.pdf") == "rechnung __.pdf"
    assert safe_name("") == "upload" and safe_name("..") == "upload" and safe_name("/") == "upload"
    long = safe_name("a" * 300 + ".json")
    assert len(long) <= 120 and long.endswith(".json")


def test_store_upload_is_content_addressed_and_idempotent(tmp_path):
    first = store_upload(tmp_path, "inv.json", b'{"a": 1}')
    assert first.parent.name == hashlib.sha256(b'{"a": 1}').hexdigest()[:12]
    assert first.name == "inv.json" and first.read_bytes() == b'{"a": 1}'
    assert store_upload(tmp_path, "inv.json", b'{"a": 1}') == first
    other = store_upload(tmp_path, "inv.json", b'{"a": 2}')
    assert other != first and first.read_bytes() == b'{"a": 1}' and other.read_bytes() == b'{"a": 2}'
    assert store_upload(tmp_path, "../../evil.json", b"{}").is_relative_to(tmp_path)


def test_store_upload_rejects_empty_files_and_bad_suffixes(tmp_path):
    with pytest.raises(ValueError):
        store_upload(tmp_path, "run.exe", b"MZ")
    with pytest.raises(ValueError):
        store_upload(tmp_path, "a.json", b"")
    with pytest.raises(ValueError):
        store_upload(tmp_path, "noext", b"x")
    assert not list(tmp_path.iterdir())


def test_batch_upload_dedupes_names_and_skips_bad_suffixes(tmp_path):
    folder = store_batch_upload(tmp_path, [("a.json", b"1"), ("a.json", b"2"), ("b.exe", b"3"), ("c.txt", b"4")])
    assert folder.parent == tmp_path and folder.name.startswith("batch-")
    assert sorted(p.name for p in folder.iterdir()) == ["a-2.json", "a.json", "c.txt"]
    again = store_batch_upload(tmp_path, [("c.txt", b"4"), ("a.json", b"1"), ("a.json", b"2")])
    assert again == folder
    with pytest.raises(ValueError):
        store_batch_upload(tmp_path, [("x.exe", b"1")])


def test_sample_files_are_sorted_and_skip_dot_files(tmp_path):
    for name in ("b.txt", "a.json", ".hidden"):
        (tmp_path / name).write_text("x")
    (tmp_path / "sub").mkdir()
    assert [p.name for p in sample_files(tmp_path)] == ["a.json", "b.txt"]
    assert sample_files(tmp_path / "missing") == []


def test_candidate_folders(tmp_path):
    data, uploads = tmp_path / "data", tmp_path / "data" / "uploads"
    (data / "invoices").mkdir(parents=True)
    (data / "invoices" / "a.txt").write_text("x")
    (data / "empty").mkdir()
    (data / "notes").mkdir()
    (data / "notes" / "readme.md").write_text("x")
    batch = store_batch_upload(uploads, [("b.csv", b"1")])
    store_upload(uploads, "single.json", b"{}")  # a single upload is not a folder of its own
    assert candidate_folders(data, uploads) == [data / "invoices", batch]
    assert candidate_folders(tmp_path / "nope", tmp_path / "nope" / "uploads") == []
