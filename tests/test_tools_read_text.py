"""The text the agent reads: tool descriptions, argument descriptions and the bundled skill."""
import re

import pytest

from fb_fakes import load_plugin, sub

plugin = load_plugin()
read = sub("tools.read")
SCHEMAS = {s["name"]: s for s in (plugin.FORGEJO_DOCTOR, plugin.FORGEJO_GET, plugin.FORGEJO_LIST,
                                  plugin.FORGEJO_COMMENT, plugin.FORGEJO_CREATE_PR, plugin.FORGEJO_STATUS)}
LIMITS = {"forgejo_doctor": 200, "forgejo_get": 200, "forgejo_list": 350, "forgejo_comment": 225,
          "forgejo_create_pr": 290, "forgejo_status": 225}
SKILL = plugin.SKILL_PATH.read_text(encoding="utf-8")


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s]


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_a_description_is_short_and_made_of_short_sentences(name):
    text = SCHEMAS[name]["description"]
    assert len(text) <= LIMITS[name], len(text)
    assert all(len(s) <= 140 for s in sentences(text)), [s for s in sentences(text) if len(s) > 140]
    assert "\u2014" not in text and "\u2013" not in text


@pytest.mark.parametrize("name,kinds", [("forgejo_get", read.GET_KINDS), ("forgejo_list", read.LIST_KINDS)])
def test_the_catalog_line_names_every_kind(name, kinds):
    """A deferred tool is picked from the first 78 characters of its description."""
    line = SCHEMAS[name]["description"][:78]
    assert all(re.search(rf"\b{kind}\b", line) for kind in kinds), line


def test_every_argument_of_forgejo_list_says_what_it_takes():
    props = plugin.FORGEJO_LIST["parameters"]["properties"]
    assert all(p.get("description") or p.get("enum") for p in props.values()), [
        k for k, p in props.items() if not (p.get("description") or p.get("enum"))]
    for flag in read.MY_FLAGS:
        assert "Hermes' account" in props[flag]["description"]


def test_the_write_note_says_when_no_one_is_asked():
    for name in ("forgejo_comment", "forgejo_create_pr", "forgejo_status"):
        text = SCHEMAS[name]["description"]
        assert "approvals are off" in text and "dry_run" in text and "Unattended runs are refused." in text


class Ctx:
    def __init__(self):
        self.skills = []

    def register_skill(self, name, path, description=""):
        self.skills.append((name, path, description))


def test_the_skill_is_registered_with_its_own_description():
    ctx = Ctx()
    plugin._register_agent_kit(ctx)
    (name, path, description), = ctx.skills
    front = re.search(r'(?m)^description:\s*"?(.*?)"?\s*$', SKILL).group(1)
    assert name == "forgebruecke" and path == plugin.SKILL_PATH and description == front and len(front) <= 90


def test_the_skill_is_short_names_every_tool_and_holds_the_rules_an_agent_needs():
    assert len(SKILL) < 3000, len(SKILL)
    for tool in ("forgejo_doctor", "forgejo_get", "forgejo_list", "forgejo_comment", "forgejo_create_pr",
                 "forgejo_status", "forgejo_flows", "forgejo_repos"):
        assert f"`{tool}`" in SKILL
    body = SKILL.split("---", 2)[2]
    assert "Words, once" not in body and "\u2014" not in body
    rules = re.sub(r"\s+", " ", body.split("## Rules")[1])
    assert all(len(s) <= 140 for s in sentences(rules))
    assert "dry_run" in rules and "nothing was written" in rules
    assert "approvals" in rules and "unasked" in rules
