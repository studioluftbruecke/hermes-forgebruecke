"""The mentions opt-out against rules that cannot be read: a rule the user wrote to switch mentions off must
not leave them on because of a typo or an odd shape."""
import pytest

from fb_fakes import sub

settings = sub("settings")
REPO = "acme/secret"


def load(writes):
    return settings.load_settings(lambda key, default=None: {"writes": writes}.get(key, default), here=lambda: "coding")


@pytest.mark.parametrize("writes", [
    [{"repo_glob": REPO, "mention": False}],
    [{"repo_glob": REPO, "Mentions": False}],
    {REPO: {"mentions": False}},
    '[{"repo_glob": "acme/secret", "mentions": false}]',
    [{"repo": REPO, "mentions": False}],
    "acme/secret: off",
    7,
], ids=["mention", "Mentions", "keyed-by-repo", "json-string", "repo-for-repo_glob", "free-text", "number"])
def test_an_opt_out_in_an_odd_shape_still_switches_mentions_off(writes):
    assert load(writes).rule_for(REPO).mentions is False


def test_a_list_stored_as_json_text_is_read_as_the_list():
    s = load('[{"repo_glob": "acme/secret", "mentions": false}, {"repo_glob": "acme/app", "flows": ["demo"]}]')
    assert s.warnings == () and len(s.writes) == 2
    assert s.rule_for("acme/app").mentions is True and s.rule_for("acme/app").flows == ("demo",)
    assert s.rule_for("acme/elsewhere").mentions is True


def test_an_empty_value_is_no_rule_and_leaves_mentions_on():
    for writes in ("", "  ", [], None):
        s = load(writes)
        assert s.warnings == () and s.rule_for(REPO).mentions is True


def test_an_unknown_key_switches_mentions_off_only_where_the_rule_does_not_state_them():
    s = load([{"repo_glob": "acme/a", "colour": "red"},
              {"repo_glob": "acme/b", "colour": "red", "mentions": True},
              {"repo_glob": "acme/c", "flows": ["demo"]}])
    assert [s.rule_for(r).mentions for r in ("acme/a", "acme/b", "acme/c", "acme/other")] == [False, True, True, True]
    assert len(s.warnings) == 2 and "unknown keys colour" in s.warnings[0]
    assert "Mentions are off for this rule" in s.warnings[0] and "Mentions are off" not in s.warnings[1]


def test_after_a_rule_that_cannot_be_read_mentions_are_off_and_flows_stay():
    s = load([{"repo_glob": "acme/first", "flows": ["demo"]},
              {"repo": REPO, "mentions": False},
              {"repo_glob": "acme/later", "mentions": True, "flows": ["demo"], "targets": ["main"]}])
    assert s.rule_for("acme/first").mentions is True
    later = s.rule_for("acme/later")
    assert later.mentions is False and later.flows == ("demo",) and later.targets == ("main",)
    assert s.rule_for(REPO).mentions is False and s.rule_for("acme/unnamed").mentions is False
    assert s.explicit_rule("acme/unnamed") is None
    assert "mentions are off wherever no earlier rule applies" in " ".join(s.warnings)


def test_every_sentence_of_a_rule_warning_is_short():
    s = load([{"repo_glob": "acme"}, "acme/y", {"repo_glob": "acme/a", "colour": "red"}])
    s2 = load("acme/secret: off")
    for warning in (*s.warnings, *s2.warnings):
        assert all(len(sentence) <= 140 for sentence in warning.split(". ")), warning
    assert len(s.warnings) == 3 and len(s2.warnings) == 1
