"""Dashboard security checks: no secret on screen, hostile document text rendered literally,
import rules between layers, and no environment loading at import time."""

import ast
import json
import re
from pathlib import Path

import pytest

from config.settings import REPO_ROOT
from llm.fake import ScriptedFake, critic, memo, vp
from models import Ceiling, Verdict
from ui.kit.runtime import UiConfig
from ui.kit.testing import app_test, build_settings, make_runtime, open_page, page_text, widget

TIMEOUT = 60
SECRET = "xai-SECRET-123"


# ------------------------------------------------------------------ (a) secrets


def test_the_api_key_never_reaches_any_page_or_the_sidebar(tmp_path):
    settings = build_settings(tmp_path).model_copy(
        update={"extraction_model": "grok-test-extract", "reasoning_model": "grok-test-reason"}
    )
    env = {"XAI_API_KEY": SECRET, "GROK_EXTRACTION_MODEL": "grok-test-extract", "GROK_REASONING_MODEL": "grok-test-reason"}
    rt = make_runtime(
        tmp_path, provider=None, env=env, settings=settings,
        default_config=UiConfig(settings.as_of, provider_choice="grok"),
    )
    at = app_test(rt, timeout=TIMEOUT)
    for page in ("process", "queue", "ledger", "batch"):
        open_page(at, page)
        assert not at.exception, [e.value for e in at.exception]
        body = page_text(at)
        assert SECRET not in body and "SECRET" not in body, page
    # the banner really did resolve to Grok (so the key was in play), and it shows the model names, not the key
    sidebar = "\n".join(str(n.proto) for n in at.sidebar if getattr(n, "proto", None) is not None)
    assert "grok-test-extract" in sidebar.replace("\\", "") and SECRET not in sidebar


# ------------------------------------------------------------ (b) hostile text

EVIL = {
    "vendor": "Evil ![x](https://example.invalid/p.png) Co $x$ :smile:",
    "notes": "[link](javascript:alert(1)) <img src=x onerror=1> <script>alert(1)</script> $x$ :tada:",
    "item": "![x](https://example.invalid/p.png) [link](javascript:alert(1)) <img src=x onerror=1> <script> $x$ :fire:",
    "note": "<img src=x onerror=1> [link](javascript:alert(1)) $x$",
}


def _evil_invoice() -> bytes:
    doc = {
        "invoice_number": "INV-9001",
        "vendor": {"name": EVIL["vendor"], "address": "1 Test Street ![x](https://example.invalid/a.png)"},
        "date": "2026-01-20", "due_date": "2026-02-20",
        "line_items": [
            {"item": EVIL["item"], "quantity": 1, "unit_price": 100.00, "note": EVIL["note"]},
            {"item": "WidgetA", "quantity": 1, "unit_price": 250.00},
        ],
        "subtotal": 350.00, "tax_amount": 0, "total": 350.00, "currency": "USD", "payment_terms": "Net 30",
        "notes": EVIL["notes"],
    }
    return json.dumps(doc).encode()


def _walk(node):
    """Every element under an AppTest block, depth first."""
    yield node
    children = getattr(node, "children", None)
    if children:
        for child in children.values():
            yield from _walk(child)


def _protos(at, kind):
    return [n.proto for n in _walk(at.main) if type(getattr(n, "proto", None)).__name__ == kind]


@pytest.fixture(scope="module")
def evil_process_page(tmp_path_factory):
    hostile = "Reject. ![x](https://example.invalid/p.png) [link](javascript:alert(1)) <img src=x onerror=1> $x$ :smile:"
    fake = ScriptedFake(fallback={
        "vp": vp(Ceiling.APPROVE, hostile),
        "vp_memo": memo(hostile),
        "critic": critic(Verdict.AGREE, Ceiling.APPROVE),
    })
    rt = make_runtime(tmp_path_factory.mktemp("evil"), provider=fake)
    at = app_test(rt, timeout=TIMEOUT)
    open_page(at, "process")
    widget(at, "process.source").set_value("upload")
    at.run()
    widget(at, "process.upload").upload("evil.json", _evil_invoice(), "application/json")
    at.run()
    widget(at, "process.run").click()
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _markdown_bodies(at):
    bodies = [p.body for p in _protos(at, "Markdown")]
    bodies += [p.body for p in _protos(at, "Heading")]
    bodies += [getattr(p, "label", "") for p in _protos(at, "Badge")]
    return bodies


def test_the_hostile_invoice_was_processed_and_its_text_is_on_the_page(evil_process_page):
    at = evil_process_page
    html_bodies = " ".join(p.body for p in _protos(at, "Html"))
    assert "example.invalid" in page_text(at), "the payload must be visible (escaped), not silently dropped"
    assert "&lt;img" in html_bodies or "&lt;script" in html_bodies


def test_no_markdown_element_contains_an_unescaped_image_or_link(evil_process_page):
    bodies = _markdown_bodies(evil_process_page)
    assert bodies
    for body in bodies:
        assert not re.search(r"(?<!\\)!\[", body), body
        assert not re.search(r"(?<!\\)\]\(", body), body
        # a backslash before `<` makes Markdown print it as text; an unescaped tag would be passed through
        assert not re.search(r"(?<!\\)<(img|script)", body, re.I), body
    joined = "\n".join(bodies)
    assert r"\<img src=x onerror=1\>" in joined, "the payload is shown, escaped"


def test_no_html_block_contains_a_raw_img_or_script_tag(evil_process_page):
    for proto in _protos(evil_process_page, "Html"):
        assert not re.search(r"<(img|script|iframe|a\s)", proto.body.replace("<style>", ""), re.I), proto.body[:300]
        # `javascript:` and `src=` may appear only as escaped text, never inside a tag
        assert not re.search(r"<[^>]*(javascript:|onerror)", proto.body, re.I), proto.body[:300]


def test_dollar_signs_never_reach_markdown_unescaped(evil_process_page):
    for body in _markdown_bodies(evil_process_page):
        assert not re.search(r"(?<!\\)\$", body), body


def test_a_shortcode_in_document_text_stays_text(evil_process_page):
    # `fmt.md` escapes the colons, so Streamlit does not turn `:fire:` into an emoji glyph in Markdown.
    # `st.html` never expands shortcodes. If Streamlit ever changes that, this is a harmless glyph, never markup.
    joined = "\n".join(_markdown_bodies(evil_process_page))
    assert not re.search(r"(?<!\\):(smile|tada|fire):", joined)
    assert r"\:fire\:" in joined
    for proto in _protos(evil_process_page, "Html"):
        assert "<img" not in proto.body.lower()


# --------------------------------------------------------------- (c) imports

ALWAYS = ("ui.kit", "models")
PAGE_EXTRA = ("core.metrics", "config.settings")
KIT_EXTRA = {
    "ui/kit/batch_view.py": ("core.metrics", "config.settings"),
    "ui/kit/case.py": ("core.text",),
    "ui/kit/data.py": ("db.repo", "graph", "tools.parsers", "config.settings"),
    "ui/kit/jobs.py": ("obs",),
    "ui/kit/process_view.py": ("llm.select",),
    "ui/kit/queue_view.py": ("service",),
    "ui/kit/runtime.py": ("service", "config.settings", "obs", "llm.replay", "llm.select", "dotenv"),
    "ui/kit/testing.py": ("service", "config.settings", "obs", "llm.fake", "llm.fake_golden"),
}
OK_THIRD_PARTY = ("streamlit", "pandas", "altair", "numpy")
FORBIDDEN_IN_PAGES = ("db", "graph", "service", "llm", "tools", "agents", "obs", "dotenv")


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
    return found


def _allowed(module: str, allowed: tuple[str, ...]) -> bool:
    top = module.split(".")[0]
    if top in OK_THIRD_PARTY or top in __import__("sys").stdlib_module_names:
        return True
    return any(module == a or module.startswith(a + ".") for a in allowed)


def _ui_files() -> list[Path]:
    return sorted((REPO_ROOT / "ui").rglob("*.py"))


def test_every_module_imports_only_what_its_layer_allows():
    problems = []
    for path in _ui_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        is_page = rel == "ui/app.py" or rel.startswith("ui/views/")
        allowed = ALWAYS + (PAGE_EXTRA if is_page else KIT_EXTRA.get(rel, ()))
        for module in _imports(path):
            if not _allowed(module, allowed):
                problems.append(f"{rel} imports {module}")
    assert not problems, "\n".join(problems)


def test_no_page_touches_storage_the_graph_or_the_service():
    pages = [REPO_ROOT / "ui/app.py", *sorted((REPO_ROOT / "ui/views").glob("*.py"))]
    assert len(pages) == 5
    for page in pages:
        for module in _imports(page):
            assert module.split(".")[0] not in FORBIDDEN_IN_PAGES, f"{page.name} imports {module}"


def test_only_the_data_module_touches_the_repo_and_the_graph():
    for path in _ui_files():
        mods = _imports(path)
        touches = {m for m in mods if m.split(".")[0] in ("db", "graph")}
        if touches:
            assert path.name == "data.py", f"{path.name} imports {touches}"


def test_ui_has_no_stray_top_level_module():
    # a file like ui/queue.py would shadow the standard library, because ui/ is on sys.path under Streamlit
    names = {p.name for p in (REPO_ROOT / "ui").glob("*.py")}
    assert names == {"__init__.py", "app.py"}


def test_nothing_outside_ui_and_tests_imports_ui():
    skip = {"ui", "tests", ".venv", "scripts", ".git", "prompts", "node_modules"}
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in skip or ".venv" in rel.parts:
            continue
        assert not any(m == "ui" or m.startswith("ui.") for m in _imports(path)), rel


# ------------------------------------------------------------ (d) dotenv


def test_runtime_has_no_module_level_load_dotenv():
    tree = ast.parse((REPO_ROOT / "ui/kit/runtime.py").read_text(encoding="utf-8"))
    for node in tree.body:  # module level only: imports, calls and assignments directly in the module body
        for sub in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)) else ():
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in sub.names] + [getattr(sub, "module", None) or ""]
                assert "load_dotenv" not in names and "dotenv" not in names, "dotenv imported at module level"
            if isinstance(sub, ast.Call):
                func = sub.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                assert name != "load_dotenv"
    # and the one call lives inside default_runtime()
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    calls = [c for c in ast.walk(funcs["default_runtime"]) if isinstance(c, ast.Call)
             and getattr(c.func, "id", "") == "load_dotenv"]
    assert calls
