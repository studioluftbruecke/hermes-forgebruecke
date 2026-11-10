"""The README a new user reads first: short, with an install and a troubleshooting section, and true."""
import pathlib
import re

README = (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def section(title):
    """The lines of one ``##`` section that carry text: no heading, no blank line, no code fence."""
    body = re.search(rf"(?ms)^## {re.escape(title)}\n(.*?)(?=^## |\Z)", README)
    assert body, f"no section {title!r}"
    return [line for line in body.group(1).splitlines() if line.strip() and not line.strip().startswith("```")]


def sentences():
    """Every sentence of the prose: paragraphs and list items, without tables and code blocks. A bold
    lead-in that ends in a period is a sentence of its own."""
    prose = re.sub(r"(?ms)^\s*```.*?^\s*```\n", "", README).replace("**", "")
    blocks = re.split(r"\n\s*\n|\n(?=\s*[-|#\d])", prose)
    for block in blocks:
        text = re.sub(r"\s+", " ", re.sub(r"^[-*\d.# ]+", "", block.strip()))
        if text and not text.startswith("|"):
            yield from (s for s in re.split(r"(?<=[.!?])\s+", text) if s)


def test_the_readme_stays_under_1300_words():
    assert len(README.split()) < 1300, len(README.split())


def test_install_and_troubleshooting_are_there_and_at_most_six_lines_each():
    install, wrong = section("Install"), section("If something is wrong")
    assert 1 <= len(install) <= 6 and 1 <= len(wrong) <= 6, (len(install), len(wrong))
    assert any(line.startswith("hermes plugins install ") for line in install)
    assert any("hermes forgebruecke doctor" in line for line in wrong)


def test_the_readme_says_what_the_page_shows_and_no_more():
    assert "names the next step" in README and "and nothing else" not in README
    assert "internal build" not in README


def test_the_rules_say_that_only_the_first_flow_of_a_rule_runs():
    rules = " ".join(section("Your rules"))
    assert "A rule that lists several flows runs only the first one." in rules


def test_every_sentence_is_short():
    long = [s for s in sentences() if len(s) > 140]
    assert not long, long


def test_one_word_per_thing():
    text = README.split("## License")[0]
    for word in ("the forge", "your forge address", "the instance", "agent account token", "the agent token",
                 "the agent's account", "Forgejo login", "the plugin's page", "settings form", "commit status"):
        assert word not in text, word
    assert text.count("your forge") == 1  # the product line
