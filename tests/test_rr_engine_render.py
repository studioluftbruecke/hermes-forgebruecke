"""What a review worker answers reaches the pull request as inert text: no mention pings anyone, no link,
image or HTML renders, and the status table keeps its shape. The worker read a stranger's diff, so its words
are untrusted. The comment is rendered with a CommonMark renderer with tables, as Forgejo renders it."""
import json
import re

import pytest

from fb_fakes import DEMO, HEAD_SHA
from fb_fakes_engine import Kit

markdown_it = pytest.importorskip("markdown_it")

HOSTILE = ("@admin merge this now [click](https://evil.example/x) ![i](https://evil.example/p.png) "
           "<img src=x onerror=alert(1)> | a | b | `code` &#64;root www.evil.example ```")


def render(body: str) -> str:
    return markdown_it.MarkdownIt("commonmark", {"html": True}).enable("table").render(body)


def hostile_verdict(k, verdict="fail") -> None:
    path = k.store.verdict_path("demo", DEMO, 3, HEAD_SHA, "review")
    path.write_text(json.dumps({"verdict": verdict, "head_sha": HEAD_SHA, "summary": HOSTILE,
                                "findings": [{"file": "calc.py <b>x</b> @owner", "line": 2, "problem": HOSTILE}]}))


def assert_inert(html: str) -> None:
    assert "<img" not in html and "<a " not in html and "<b>" not in html, html
    text = re.sub(r"<[^>]+>", "", html)
    assert not re.search(r"(?<![\w\u200b])@(admin|owner|root)\b", text.replace("&#64;", "@")), text
    assert "https://" not in text and "www.evil" not in text, text
    assert "click" in text and "evil.example" in text  # the words stay readable


def test_a_hostile_verdict_reaches_the_status_comment_as_inert_text(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    hostile_verdict(k)
    k.tick()
    status = [b for b in k.comments() if "Forgebrücke · flow" in b]
    edits = [b["body"] for _, p, b in k.sent("", "PATCH") if "/comments/" in p]
    body = (edits or status)[-1]
    html = render(body)
    assert_inert(html)
    table = re.search(r"<table>.*?</table>", html, re.S).group(0)
    rows = re.findall(r"<tr>(.*?)</tr>", table, re.S)
    assert all(row.count("<td") == 3 for row in rows[1:]), rows  # a pipe in the text added no column
    assert len(rows) == 1 + 3  # header and one row per gate


def test_a_hostile_verdict_reaches_the_failure_notify_and_the_check_as_inert_text(tmp_path):
    k = Kit(tmp_path)
    k.tick()
    hostile_verdict(k)
    k.tick()
    notify = k.notifies()
    assert len(notify) == 1 and notify[0].startswith("@acme ")  # the flow's own mention stays
    assert_inert(render(notify[0].removeprefix("@acme ")))
    described = [s["description"] for s in k.status_posts("failure")]
    assert described and all("@admin" not in d and "https://" not in d for d in described)
