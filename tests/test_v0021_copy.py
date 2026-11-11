"""0.0.21 part 1, M3. The plan's grep "bridge to your forge" with -i also matches the new "bridge to your
Forgejo", so this one ends the old word at a word boundary.

M3 (PLAN-v0.0.20.md V9 to V11): the copy sweep. His tagline, no claim that every write asks,
and the write token's risk in his words, one source for the settings page and the manifest."""
import re
import subprocess

from fb_fakes import ROOT, sub

REPO = ROOT.parents[1]
SOURCE = (ROOT / "desktop" / "plugin.js").read_text(encoding="utf-8")
MANIFEST = (ROOT / "plugin.yaml").read_text(encoding="utf-8")


def test_the_old_claims_are_gone_from_the_plugin_and_the_launch_texts():
    paths = ["plugins/forgebruecke", "experiments/forgebruecke/BRAND.md", "experiments/forgebruecke/PRODUCT.md",
             "experiments/forgebruecke/launch"]
    if not (REPO / ".git").exists():
        paths = ["."]
    out = subprocess.run(["git", "grep", "-i", "-l", "-P", "-e", "approve each write", "-e", "approve every write",
                          "-e", "bridge to your forge\\b", "--", *paths, ":!*/tests/*"],
                         cwd=REPO if (REPO / ".git").exists() else ROOT, capture_output=True, text=True)
    assert out.stdout == "", out.stdout
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
