"""The texts of the write, flows and repos tools: publishable, and found by their first line in the catalog."""
import pathlib
import re

import pytest

from fb_fakes import BASE, DEMO, sub
from fb_fakes_tools_write import OTHER, FlowsKit, Kit

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULES = ("tools/write.py", "tools/flows.py", "tools/repos.py")
# ids from the team's plan, dates and versions mean nothing to a reader of the published plugin
INTERNAL = re.compile(r"\bK-F\d+\b|\bD\d{2}\b|\bM\d+[a-z]?\b|\b20\d\d-\d\d-\d\d\b|\b0\.0\.\d+\b")


@pytest.mark.parametrize("module", MODULES)
def test_a_tool_module_carries_no_plan_id_date_or_version(module):
    assert INTERNAL.findall((ROOT / module).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("module,budget", [("tools.flows", 500), ("tools.repos", 430)])
def test_the_catalog_line_names_every_action_and_the_description_stays_short(module, budget):
    schema = sub(module).SCHEMA
    line = schema["description"][:78]  # what Hermes' deferred tool catalog shows of a description
    actions = schema["parameters"]["properties"]["action"]["enum"]
    assert [a for a in actions if not re.search(rf"\b{a}\b", line)] == []
    assert schema["description"].index(". ") < 78
    assert len(schema["description"]) <= budget


def test_no_argument_description_of_forgejo_repos_is_longer_than_a_line():
    props = sub("tools.repos").SCHEMA["parameters"]["properties"]
    assert {name: len(p["description"]) for name, p in props.items() if len(p.get("description", "")) > 100} == {}


def longest_sentence(text: str) -> int:
    """A link has the length of the user's host name, so it counts as one word."""
    plain = re.sub(rf"{re.escape(BASE)}\S*", "<link>", text)
    return max(len(part) for part in re.split(r"(?<=[.!?])\s+", plain))


def refusals_a_user_reads(tmp_path):
    repos, flows_tool = sub("tools.repos"), sub("tools.flows")
    spec = repos.make_spec()
    k = Kit(settings={"audit_enabled": True})
    k.forge.data[DEMO]["branches"]["x"] = {"name": "x"}
    yield "target without a rule", k.write("forgejo_create_pr", {"repo": DEMO, "head": "x", "base": "next",
                                                                 "title": "t"})[0]["message"]
    ruled = Kit(settings={"writes": [{"repo_glob": DEMO, "targets": ["main", "staging"]}]})
    ruled.forge.data[DEMO]["branches"]["x"] = {"name": "x"}
    yield "target outside the rule", ruled.write("forgejo_create_pr", {"repo": DEMO, "head": "x", "base": "next",
                                                                       "title": "t"})[0]["message"]
    yield "preset without admin", k.write("forgejo_repos", {"action": "preset", "repo": DEMO}, spec=spec)[0]["message"]
    yield "mirror without admin", k.write("forgejo_repos", {"action": "mirror", "repo": DEMO, "target": OTHER},
                                          spec=spec)[0]["message"]
    yield "template without the scope", template_refusal(spec)
    yield "protection without admin", repos.overview(k.load(), DEMO)["protection"]["unknown"]
    yield "audit without admin", repos.audit(k.load(), DEMO, now=0.0)["not_checked"][0]
    admin = Kit(other="admin")
    yield "mirror card", spec.plan(admin.load(), {"action": "mirror", "repo": OTHER, "target": DEMO}).summary
    f = FlowsKit(tmp_path, settings={"writes": []})
    yield "flow the rule does not list", flows_tool.make_spec(lambda: f.store).plan(
        f.load(), {"action": "set", "repo": DEMO, "template": "solo-with-agents", "target": "local"}).summary


def template_refusal(spec) -> str:
    k = Kit()
    k.forge.data[OTHER]["info"].update(template=True, id=77)
    return k.write("forgejo_repos", {"action": "create_from_template", "repo": OTHER, "name": "fresh"},
                   spec=spec)[0]["message"]


def test_no_sentence_of_a_refusal_or_card_is_longer_than_140_characters(tmp_path, monkeypatch):
    monkeypatch.setattr(sub("settings"), "this_profile", lambda: "coding")
    lengths = {name: longest_sentence(text) for name, text in refusals_a_user_reads(tmp_path)}
    assert len(lengths) == 9
    assert {name: n for name, n in lengths.items() if n > 140} == {}
