"""Layering guard: nothing outside ui/ and tests/ may import the ui package."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_TOP = {"ui", "tests", ".venv", ".git", "node_modules", "__pycache__"}


def _files():
    for p in ROOT.rglob("*.py"):
        rel = p.relative_to(ROOT)
        if rel.parts[0] in SKIP_TOP or any(part in SKIP_TOP for part in rel.parts):
            continue
        if rel.parts[0] == "scripts" and rel.name.startswith("ui_"):
            continue
        yield p


def _imports_ui(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "ui" or a.name.startswith("ui.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "ui" or node.module.startswith("ui."):
                return True
    return False


def test_nothing_outside_ui_imports_ui():
    offenders = [str(p.relative_to(ROOT)) for p in _files() if _imports_ui(ast.parse(p.read_text(encoding="utf-8")))]
    assert offenders == []


def test_scanner_detects_both_import_forms():
    assert _imports_ui(ast.parse("import ui.kit"))
    assert _imports_ui(ast.parse("from ui import app"))
    assert _imports_ui(ast.parse("from ui.kit import x"))
    assert not _imports_ui(ast.parse("import uix\nfrom models import ui"))
