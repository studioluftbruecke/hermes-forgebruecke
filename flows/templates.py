"""The three flow templates, as data.

``render(name, repo, review_profile, owner)`` returns the file shape (one-key gate maps, the way a
person writes ``.forgebruecke/flows.yml``); ``models.flow_to_yaml`` turns it into the file text.
"""
from __future__ import annotations

from typing import Optional

TEMPLATES = {
    "solo-with-agents": {
        "summary": "His own path: an agent review, the checks devops posts, then you merge.",
        "gates": [
            {"review": {"by": "{review_profile}", "lens": "correctness", "max_fix_rounds": 3}},
            {"check": {"contexts": ["devops/tests", "devops/review"], "timeout_min": 120}},
            {"human": {"who": "{owner}", "action": "merge"}},
        ],
        "on_failure": "notify",
    },
    "one-review-then-me": {
        "summary": "For beginners: one agent review, then you merge. Nothing else to set up.",
        "gates": [
            {"review": {"by": "{review_profile}", "lens": "correctness", "max_fix_rounds": 2}},
            {"human": {"who": "{owner}", "action": "merge"}},
        ],
        "on_failure": "notify",
    },
    "staging-first": {
        "summary": "A review, then the change goes to staging and waits for the deploy check, then you merge.",
        "gates": [
            {"review": {"by": "{review_profile}", "lens": "correctness", "max_fix_rounds": 3}},
            {"promote": {"to": "staging", "then": "verify_deploy", "contexts": ["deploy/*"], "timeout_min": 30}},
            {"human": {"who": "{owner}", "action": "merge"}},
        ],
        "on_failure": "notify",
    },
}


def _fill(value, subs: dict):
    if isinstance(value, str):
        for k, v in subs.items():
            value = value.replace("{" + k + "}", v)
        return value
    if isinstance(value, list):
        return [_fill(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: _fill(v, subs) for k, v in value.items()}
    return value


def names() -> list:
    return [{"name": n, "summary": t["summary"]} for n, t in TEMPLATES.items()]


def render(name: str, repo: str, review_profile: str, owner: str, flow_id: Optional[str] = None) -> dict:
    if name not in TEMPLATES:
        raise KeyError(name)
    t = TEMPLATES[name]
    owner = (owner or repo.split("/")[0]).lstrip("@")
    subs = {"review_profile": review_profile, "owner": owner}
    return {"flow": flow_id or name, "name": name.replace("-", " ").capitalize(), "repos": [repo],
            "gates": _fill(t["gates"], subs), "on_failure": t["on_failure"]}
