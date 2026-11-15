"""Dashboard entry point: `uv run streamlit run ui/app.py`.

Pages live in `ui/views/`, not `ui/pages/`: a `pages/` folder next to the entry script makes
Streamlit fall back to its legacy page navigation on a fresh server's first deep link.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if sys.path[:1] != [str(REPO_ROOT)]:
    sys.path.insert(0, str(REPO_ROOT))

import streamlit as st  # noqa: E402

from ui.kit import theme  # noqa: E402
from ui.kit.runtime import get_runtime  # noqa: E402
from ui.kit.sidebar import render_sidebar, session_config  # noqa: E402

st.set_page_config(page_title="Acme accounts payable", layout="wide", initial_sidebar_state="auto")

rt = get_runtime()
with rt.reader(session_config(rt)) as reader:
    pending = len(reader.queue("PENDING"))

nav = st.navigation(
    [
        st.Page("views/1_Process.py", title="Process", icon=":material/description:", url_path="process", default=True),
        st.Page(
            "views/2_Review_queue.py", title=f"Review queue ({pending})" if pending else "Review queue",
            icon=":material/pending_actions:", url_path="queue",
        ),
        st.Page("views/3_Ledger.py", title="Ledger", icon=":material/menu_book:", url_path="ledger"),
        st.Page("views/4_Batch.py", title="Batch", icon=":material/stacks:", url_path="batch"),
    ],
    position="top",
)
with st.sidebar:
    theme.style()  # shared CSS for the books; each page also calls style() at its top
render_sidebar(rt)
nav.run()
