"""The copy: the tagline, no claim that every write asks, and the write token's risk in one sentence, one
source for the settings page and the manifest. The old "bridge to your forge" ends at a word boundary, so the
new "bridge to your Forgejo" passes."""
import re

from fb_fakes import ROOT, sub

SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
MANIFEST = (ROOT / "plugin.yaml").read_text(encoding="utf-8")


def test_the_old_claims_are_gone_from_the_plugin_and_the_launch_texts():
    old = re.compile(r"approve each write|approve every write|bridge to your forge\b", re.I)
    said = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file() and "tests" not in p.relative_to(ROOT).parts
            and p.suffix in (".py", ".js", ".md", ".yaml") and old.search(p.read_text(encoding="utf-8", errors="replace"))]
    assert said == [], said
    assert "**A bridge to your Forgejo.**" in (ROOT / "README.md").read_text(encoding="utf-8")


def test_the_write_tokens_risk_is_one_sentence_on_the_page_and_in_the_manifest():
    risk = sub("capabilities").OWNER_RISK
    assert risk == "It is also exposed to Hermes, ultimately giving Hermes full access to your account."
    rule = " ".join(re.findall(r"'([^']+)'", SOURCE[SOURCE.index("['eye', "):SOURCE.index("['pass', ", SOURCE.index("['eye', "))]))
    assert rule.endswith(risk[0].lower() + risk[1:]), rule
    owner = re.search(r"owner_token:.*?description: \"([^\"]+)\"", MANIFEST, re.S).group(1)
    assert owner.endswith("; " + risk[0].lower() + risk[1:]) and len(owner) <= 140, (len(owner), owner)


def test_no_text_says_the_human_approves_every_write():
    assert "Each follows Hermes’ approval setting" in SOURCE
    assert "writes follow Hermes' approval setting" in MANIFEST
    assert "You approve each one in the chat" not in SOURCE
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "shown to you first" not in readme and "after your approval" not in readme
    assert "Writes follow Hermes' approval setting" in readme
