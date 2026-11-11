"""The text a user reads in Hermes' own settings form and on the plugin's page: short, one sentence each."""
import re
import types

from fb_fakes import ROOT, sub

caps = sub("capabilities")
MANIFEST = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
FIELDS = dict((key, (label, text)) for key, label, text in re.findall(
    r'^  (\w+):\n(?:    (?:type|env|default): .*\n)+    label: "([^"]*)"\n    description: "([^"]*)"', MANIFEST, re.M))
# Their exact text is asserted by tests of the doctor and of access; they shorten together with those.
PINNED_LABELS = {"writes", "flows", "owner_merge"}


def words(text):
    return len(text.split())


def one_sentence(text):
    return text.endswith(".") and not re.search(r"[.!?] [A-Z]", text)


def test_the_manifest_describes_the_plugin_in_under_200_characters():
    description = re.search(r'^description: "([^"]*)"', MANIFEST, re.M).group(1)
    assert 60 < len(description) < 200 and "\u2014" not in description and "\u2013" not in description


def test_every_setting_is_one_sentence_of_at_most_140_characters():
    assert len(FIELDS) == 13
    for key, (label, text) in FIELDS.items():
        assert len(text) <= 140, (key, len(text))
        assert one_sentence(text), (key, text)
        assert not re.match(r"(Step \d|Optional[,.])", text), (key, text)
        assert words(label) <= 12 and "\u2014" not in label + text and "\u2013" not in label + text, key


def test_setting_labels_say_what_the_user_gets():
    assert FIELDS["dry_run"][0] == "Preview only (Hermes writes nothing)"
    assert "forge " not in " ".join(label for label, _text in FIELDS.values()) and "lights" not in MANIFEST
    assert "{" not in FIELDS["writes"][1] and "for all repositories" in FIELDS["token"][1]
    assert FIELDS["writes"][0] == "Flows and mentions per repo"
    assert FIELDS["review_every_pr"][0] == "Ask me to review every pull request"
    assert "Per-repo rules" not in MANIFEST and '"Dry run"' not in MANIFEST


def test_feature_labels_are_at_most_five_words():
    for fid, label, _needs, _off in caps.FEATURES:
        if fid not in PINNED_LABELS:
            assert words(label) <= 5, (fid, label)


def settings(**kw):
    base = {"dry_run": False, "audit_enabled": True, "writes": (types.SimpleNamespace(flows=("demo",)),)}
    return types.SimpleNamespace(**{**base, **kw})


def reason(fid, found, settings_):
    return next(f for f in caps.resolve(found, settings_) if f["id"] == fid)


ALL = {cap: caps.FIELD_FOR[cap] for cap in caps.CAPS}


def test_an_off_feature_says_in_at_most_14_words_how_to_switch_it_on():
    off = {"writes": settings(dry_run=True), "flows": settings(writes=()), "audit": settings(audit_enabled=False)}
    for fid, settings_ in off.items():
        entry = reason(fid, ALL, settings_)
        assert entry["status"] == "off" and words(entry["reason"]) <= 14, entry
        assert re.search(r"\b(Switch|Add)\b", entry["reason"]), entry
    assert "Let Hermes write" in reason("writes", ALL, off["writes"])["reason"]
    for fid in off:
        assert "plugin settings" not in reason(fid, ALL, off[fid])["reason"]


def test_a_feature_this_forgejo_does_not_offer_lists_what_is_missing():
    entry = next(f for f in caps.resolve(ALL, settings(), ops_missing=lambda fid: ["POST /repos/migrate"] if fid == "import" else [])
                 if f["id"] == "import")
    assert entry["status"] == "dark" and entry["missing"] == ["POST /repos/migrate"]
    assert entry["reason"] == "This Forgejo version does not offer it."


def test_a_missing_capability_is_named_in_at_most_14_words():
    for cap in caps.CAPS:
        assert words(caps.needs_sentence([cap])) <= 14, cap
    entry = reason("owner_merge", {k: v for k, v in ALL.items() if k != "owner:merge"}, settings())
    assert entry["status"] == "dark" and entry["reason"].startswith("Needs your write token")


def test_a_feature_that_lacks_two_things_of_one_token_names_the_token_once():
    entry = reason("mentions", {"read:repos": "bot"}, settings())
    assert entry["reason"] == ("Needs Hermes' token with read:notification, write:issue and write:repository, for all "
                               "repositories.")
    assert entry["light_with"] == ["bot", "bot"] and "milestone" not in entry


def test_the_registry_names_no_token_by_its_field_id_except_where_another_test_still_pins_it():
    spoken = [caps.needs_sentence([cap]) for cap in caps.CAPS if cap not in ("owner:merge", "owner:comment")]
    spoken += [t[key] for t in caps.TOKENS for key in ("name", "unlocks", "never")]
    spoken += [s[key] for s in caps.SETUPS for key in ("label", "summary")] + [label for _i, label, _n, _o in caps.FEATURES]
    for text in spoken:
        assert "FORGEBRUECKE_" not in text and "agent account" not in text.lower() and "PRs" not in text, text


def test_token_rows_say_what_they_unlock_never_do_and_risk_in_one_short_sentence_each():
    for row in caps.TOKENS:
        for key in ("unlocks", "never", "risk"):
            if key in row:
                assert one_sentence(row[key]) and len(row[key]) <= 100, (row["role"], key)
    for setup in caps.SETUPS:
        assert len(setup["summary"]) <= 140, setup["id"]
