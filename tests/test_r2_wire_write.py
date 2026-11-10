"""The write tools' result when the first request fails: whether it was sent, and where to look for it."""
import pytest

from fb_fakes import BASE, DEMO, HEAD_SHA, FakeForgejo, bot_account, make_token, sample_data, sub

approval = sub("approval")
access_mod = sub("access")
client = sub("client")
read = sub("tools.read")
write = sub("tools.write")
BOT = make_token("bot")
ATTENDED = approval.RunContext()
CALLS = [
    ("forgejo_comment", {"repo": DEMO, "number": 3, "body": "Looks right now."}, "comments"),
    ("forgejo_comment", {"repo": DEMO, "number": 3, "body": "Off by one.", "path": "calc.py", "line": 2}, "reviews"),
    ("forgejo_create_pr", {"repo": DEMO, "head": "fix-mul", "title": "Fix mul"}, "prs"),
    ("forgejo_status", {"repo": DEMO, "sha": HEAD_SHA, "state": "success"}, "statuses"),
]


class FirstWrite(FakeForgejo):
    """``fate(real)`` decides what the tool's first write answers; ``real()`` lets the forge handle it."""

    def __init__(self, fate):
        data = sample_data()
        data[DEMO]["branches"]["fix-mul"] = {"name": "fix-mul", "commit": {"id": HEAD_SHA}}
        super().__init__({BOT: bot_account()}, data=data)
        self.fate = fate

    def __call__(self, method, url, headers, body, timeout):
        if method == "GET":
            return super().__call__(method, url, headers, body, timeout)
        return self.fate(lambda: FakeForgejo.__call__(self, method, url, headers, body, timeout))


def run(forge, tool, args):
    values = {"url": BASE, "dry_run": False}

    def load():
        return access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                      transport=forge)

    tickets = approval.ApprovalTickets()
    gate = approval.make_gate(load, lambda: ATTENDED, tickets)
    assert gate(tool_name=tool, args=args, session_id="s")["action"] == "approve"
    return approval.run_write(tool, args, "s", load, ATTENDED, tickets)


def _stored_then_lost(real):
    real()
    raise client.ForgejoError(None, "no_answer", f"Forgejo's answer did not arrive (closed). {client.MAY_HAVE_LANDED}",
                              may_have_landed=True)


def _stored_then_502(real):
    real()
    return 502, {}, b""


def _refused_connection(real):
    raise client.ForgejoError(None, "unreachable", "Forgejo is unreachable (Connection refused); nothing was changed.")


@pytest.mark.parametrize("fate,code,status", [(_stored_then_lost, "no_answer", None),
                                              (_stored_then_502, "unreachable", 502)])
@pytest.mark.parametrize("tool,args,list_kind", CALLS)
def test_a_write_whose_answer_was_lost_names_the_list_that_shows_it(tool, args, list_kind, fate, code, status):
    forge = FirstWrite(fate)
    out = run(forge, tool, args)
    assert len(forge.writes) == 1
    assert (out["ok"], out["sent"], out["error"], out["status"]) == (False, True, code, status)
    assert out["message"].endswith(f"{client.MAY_HAVE_LANDED} forgejo_list kind={list_kind} shows it.")
    assert list_kind in read.LIST_KINDS and "nothing was changed" not in out["message"]
    assert out["summary"]


@pytest.mark.parametrize("tool,args,_list_kind", CALLS)
def test_a_write_that_never_connected_is_answered_not_sent(tool, args, _list_kind):
    forge = FirstWrite(_refused_connection)
    out = run(forge, tool, args)
    assert forge.writes == []
    assert (out["ok"], out["sent"], out["error"]) == (False, False, "unreachable")
    assert "nothing was changed" in out["message"] and "forgejo_list" not in out["message"]


def test_a_write_the_forge_refused_is_answered_sent_without_a_list_to_check():
    forge = FirstWrite(lambda real: (403, {}, b'{"message": "no"}'))
    out = run(forge, *CALLS[0][:2])
    assert (out["ok"], out["sent"], out["error"], out["status"]) == (False, True, "forbidden", 403)
    assert "may have landed" not in out["message"] and "forgejo_list" not in out["message"]
