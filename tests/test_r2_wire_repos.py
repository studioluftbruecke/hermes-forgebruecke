"""forgejo_repos: a write whose answer was lost counts as sent, and an import is then looked for."""
import pytest

from fb_fakes import DEMO, sub
from test_repos import OWN, approved_run, forge

repos = sub("tools.repos")
client = sub("client")
GH = "https://github.com/octocat/git-consortium"


def _answer_lost(f, suffix, how):
    """``f`` handles the write that ends in ``suffix``; its answer is a proxy's 504 or never arrives."""
    def send(method, url, headers, body, timeout):
        if method != "GET" and url.endswith(suffix):
            f(method, url, headers, body, timeout)
            if how == "proxy gave up":
                return 504, {}, b""
            raise client.ForgejoError(None, "no_answer", f"Forgejo's answer did not arrive. {client.MAY_HAVE_LANDED}",
                                      may_have_landed=True)
        return f(method, url, headers, body, timeout)
    return send


@pytest.mark.parametrize("how", ["proxy gave up", "connection closed"])
def test_an_import_whose_answer_was_lost_is_looked_for(how, monkeypatch):
    monkeypatch.setattr(repos, "_sleep", lambda s: None)
    _, out = approved_run(_answer_lost(forge(), "/repos/migrate", how), {"action": "import", "source": GH})
    assert out["ok"] and out["sent"] and out["came_over"]["issues"] == 3
    assert "took longer than one request" in out["message"]


@pytest.mark.parametrize("how,code", [("proxy gave up", "unreachable"), ("connection closed", "no_answer")])
def test_a_mirror_whose_answer_was_lost_is_answered_sent(how, code):
    f = forge()
    _, out = approved_run(_answer_lost(f, "/push_mirrors", how), {"action": "mirror", "repo": OWN, "target": DEMO})
    assert len([w for w in f.writes if w[1].endswith("/push_mirrors")]) == 1
    assert out["ok"] is False and out["sent"] is True and out["error"] == code
    assert "The write may have landed. Check before you repeat it." in out["message"]
