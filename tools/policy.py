"""Policy lookup tool; topics are the `##` headings of policy.md."""

from pathlib import Path

from deps import Deps
from tools.registry import tool


def _sections(path: Path) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def policy_topics(path: Path) -> list[str]:
    return list(_sections(path))


@tool()
def get_policy(deps: Deps, topic: str) -> dict:
    """Return the text of one policy topic (for example thresholds, vendors, budget)."""
    sections = _sections(deps.settings.policy_path)
    key = topic.strip().lower()
    if key in sections:
        return {"topic": key, "text": sections[key]}
    return {"error": f"unknown policy topic {topic!r}", "topics": list(sections)}
