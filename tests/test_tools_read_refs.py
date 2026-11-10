"""A ref goes into the request path, so it is checked before any request, and the refusal says what is wrong."""
import pytest

from fb_fakes import DEMO, HEAD_SHA
from fb_fakes_tools_read import OTHER, mention, read


@pytest.mark.parametrize("kind", ["release", "branch", "commit"])
@pytest.mark.parametrize("ref,wrong", [
    (f"../../../../{OTHER}/issues/1", ".."), ("a..b", ".."), ("a b", " "), ("v1\n", None), ("x?y=1", "?"),
    ("a#b", "#"), ("./x", "."), ("a/.hidden", "."), ("/abs", "/"), ("a//b", "//"), ("%2e%2e", "%"),
])
def test_a_ref_that_cannot_be_a_git_name_is_refused_before_any_request(kind, ref, wrong):
    out, forge = read("forge_get", {"kind": kind, "repo": DEMO, "ref": ref})
    if wrong is None:  # surrounding whitespace is trimmed, as before
        assert out.get("error") != "bad_args"
        return
    assert out["ok"] is False and out["error"] == "bad_args"
    assert repr(wrong) in out["message"] and "Give the" not in out["message"]
    assert not [c for c in forge.calls if "/releases" in c[1] or "/branches" in c[1] or "/git/" in c[1]]


def test_a_mention_turn_cannot_reach_another_repo_through_a_release_tag():
    out, forge = read("forge_get", {"kind": "release", "repo": DEMO, "ref": f"../../../../{OTHER}/issues/1"},
                      lane=mention(DEMO).lane())
    assert out["error"] == "bad_args" and not [c for c in forge.calls if OTHER in c[1]]


def test_real_names_still_pass_and_a_missing_ref_is_asked_for():
    out, _ = read("forge_get", {"kind": "branch", "repo": DEMO, "ref": "main"})
    assert out["ok"] and out["branch"]["name"] == "main"
    out, forge = read("forge_get", {"kind": "branch", "repo": DEMO, "ref": "feat/x-1.2_y"})
    assert [c for c in forge.calls if c[1].endswith("/branches/feat/x-1.2_y")]
    out, _ = read("forge_get", {"kind": "branch", "repo": DEMO})
    assert out["error"] == "bad_args" and "Give the branch name" in out["message"]
    out, forge = read("forge_get", {"kind": "release", "repo": DEMO})
    assert [c for c in forge.calls if c[1].endswith("/releases/latest")]
    out, forge = read("forge_list", {"kind": "statuses", "repo": DEMO, "ref": "a b"})
    assert out["error"] == "bad_args" and not [c for c in forge.calls if "/commits/" in c[1]]
    out, _ = read("forge_list", {"kind": "statuses", "repo": DEMO, "ref": HEAD_SHA})
    assert out["ok"]
