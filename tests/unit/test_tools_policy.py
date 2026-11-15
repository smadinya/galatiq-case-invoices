import re

from config.settings import REPO_ROOT
from tools.policy import get_policy, policy_topics

POLICY = REPO_ROOT / "policy.md"


def test_topics_match_headings():
    expected = [h.strip().lower() for h in re.findall(r"^## (.+)$", POLICY.read_text(), re.M)]
    assert policy_topics(POLICY) == expected and "thresholds" in expected


def test_every_topic_returns_text(make_deps):
    deps = make_deps()
    for topic in policy_topics(POLICY):
        out = get_policy(deps, topic)
        assert out["topic"] == topic and out["text"].strip() and not out["text"].startswith("## ")


def test_topic_case_insensitive_and_content(make_deps):
    out = get_policy(make_deps(), " Thresholds ")
    assert out["topic"] == "thresholds" and "$10,000" in out["text"]
    assert "## severities" not in out["text"]


def test_unknown_topic(make_deps):
    out = get_policy(make_deps(), "nonsense")
    assert "error" in out and out["topics"] == policy_topics(POLICY) and "text" not in out
