"""The text the agent reads on every turn: the eight registered descriptions and the words in them."""
import re

import pytest

from fb_fakes import load_plugin, sub

plugin = load_plugin()
MINE = ("forgejo_doctor", "forgejo_get", "forgejo_list", "forgejo_comment", "forgejo_create_pr", "forgejo_status",
        "forgejo_repos")


class Ctx:
    """Records the tools ``register`` registers and accepts every other call."""

    def __init__(self):
        self.schemas = {}

    def register_tool(self, **kw):
        self.schemas[kw["name"]] = kw["schema"]

    def get_config(self, key, default=None):
        return default

    def __getattr__(self, name):
        return lambda *a, **k: None


def registered():
    ctx = Ctx()
    plugin.register(ctx)
    return ctx.schemas


SCHEMAS = registered()


def texts(schema):
    """The description of a tool and of each of its arguments."""
    yield schema["description"]
    for prop in schema["parameters"]["properties"].values():
        if prop.get("description"):
            yield prop["description"]


def test_the_eight_registered_descriptions_total_at_most_2400_characters():
    sizes = {name: len(schema["description"]) for name, schema in SCHEMAS.items()}
    assert len(sizes) == 8 and sum(sizes.values()) <= 2400, sizes


@pytest.mark.parametrize("name", MINE)
def test_a_description_is_made_of_short_sentences(name):
    long = [s for s in re.split(r"(?<=[.!?])\s+", SCHEMAS[name]["description"]) if len(s) > 140]
    assert not long, long


@pytest.mark.parametrize("name", MINE)
def test_the_agent_reads_one_word_per_thing(name):
    for text in texts(SCHEMAS[name]):
        assert not re.search(r"\b[Ff]orge\b", text), text
        assert "Forgejo account" not in text and "agent account" not in text and "login" not in text, text


def test_the_list_description_says_what_the_desk_is_and_that_it_pages():
    text = SCHEMAS["forgejo_list"]["description"]
    assert text.startswith("List prs, issues, repos, reviews, statuses, comments or the desk, a page each.")
    assert "desk: what waits for the user" in text


def test_the_repos_description_says_which_actions_write():
    text = SCHEMAS["forgejo_repos"]["description"]
    assert "The other five write and ask the user first." in text
    for action in sub("tools.repos").ACTIONS:
        assert action in text[:78] or f"{action}:" in text, action


def test_the_skill_is_no_longer_than_before_and_says_one_word_per_thing():
    skill = plugin.SKILL_PATH.read_text(encoding="utf-8")
    body = skill.split("---", 2)[2]
    assert len(skill) <= 2409, len(skill)
    assert not re.search(r"\b[Ff]orge\b", body) and "Forgejo account" not in body and "Dry run" not in body
    assert body.count("setup") == 1  # the doctor's row. The repos row names its actions instead


def test_a_rule_the_tool_already_says_at_the_call_is_not_in_the_skill():
    """The list flags and the default reviewer are in the tools' descriptions and answers."""
    rules = plugin.SKILL_PATH.read_text(encoding="utf-8").split("## Rules")[1]
    assert "relative_to" not in rules and "`reviewers`" not in rules
    assert "Hermes' account, not the user's" in SCHEMAS["forgejo_list"]["description"]
    assert "reviewers defaults to the user" in SCHEMAS["forgejo_create_pr"]["description"]
