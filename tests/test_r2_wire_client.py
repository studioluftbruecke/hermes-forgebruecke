"""A write whose answer never arrived: sent, may have landed, and never "nothing was changed"."""
import email.message
import http.client
import io
import socket
import urllib.error

import pytest

from fb_fakes import DEMO, sub
from fb_fakes_wire import BOT, LoopbackForge, closed_port

client = sub("client")
approval = sub("approval")
access_mod = sub("access")
ATTENDED = approval.RunContext()
COMMENT = {"repo": DEMO, "number": 3, "body": "Looks right now."}
URL = "https://forge.example.test/api/v1/repos/a/b/issues/1/comments"
WRITES = ("POST", "PATCH", "PUT", "DELETE")


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")


class _Answer:
    """What urllib hands back for a 200 whose body ends before its Content-Length."""
    status, headers = 200, {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise http.client.IncompleteRead(b"", 40)


class _CutBody(io.BytesIO):
    def read(self, *args):
        raise http.client.IncompleteRead(b"", 40)


def _raises(error):
    def open_url(req, timeout=None):
        raise error
    return open_url


def _error_status_with_a_cut_body(req, timeout=None):
    raise urllib.error.HTTPError(URL, 502, "Bad Gateway", email.message.Message(), _CutBody())


ANSWER_LOST = {
    "the connection closed without an answer": _raises(http.client.RemoteDisconnected("Remote end closed connection "
                                                                                       "without response")),
    "the connection was reset": _raises(ConnectionResetError(104, "Connection reset by peer")),
    "the status line was garbage": _raises(http.client.BadStatusLine("\x00")),
    "the body was cut short": lambda req, timeout=None: _Answer(),
    "the body of an error status was cut short": _error_status_with_a_cut_body,
    "the request went out and the pipe broke": _raises(urllib.error.URLError(BrokenPipeError(32, "Broken pipe"))),
}
TIMED_OUT = {
    "the read timed out": _raises(TimeoutError("The read operation timed out")),
    "urllib wrapped the timeout": _raises(urllib.error.URLError(TimeoutError("timed out"))),
}
NEVER_CONNECTED = {
    "the connection was refused": _raises(urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))),
    "the name did not resolve": _raises(urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))),
}


def _error(method, open_url):
    with pytest.raises(client.ForgejoError) as e:
        client.urllib_transport(method, URL, {}, None if method == "GET" else b"{}", 1, open_url=open_url)
    return e.value


@pytest.mark.parametrize("method", WRITES)
@pytest.mark.parametrize("case", [*ANSWER_LOST, *TIMED_OUT])
def test_a_write_without_an_answer_may_have_landed(method, case):
    e = _error(method, {**ANSWER_LOST, **TIMED_OUT}[case])
    assert e.kind == ("timeout" if case in TIMED_OUT else "no_answer") and e.status is None
    assert e.may_have_landed and e.sent
    assert "The write may have landed. Check before you repeat it." in e.message
    assert "nothing was changed" not in e.message


@pytest.mark.parametrize("method", WRITES)
@pytest.mark.parametrize("case", list(NEVER_CONNECTED))
def test_a_write_that_never_connected_changed_nothing(method, case):
    e = _error(method, NEVER_CONNECTED[case])
    assert e.kind == "unreachable" and e.status is None
    assert not e.may_have_landed and not e.sent
    assert "nothing was changed" in e.message and "may have landed" not in e.message


@pytest.mark.parametrize("case", [*ANSWER_LOST, *TIMED_OUT, *NEVER_CONNECTED])
def test_a_read_without_an_answer_is_unreachable(case):
    e = _error("GET", {**ANSWER_LOST, **TIMED_OUT, **NEVER_CONNECTED}[case])
    assert e.kind == "unreachable" and e.status is None
    assert not e.may_have_landed and "may have landed" not in e.message and "nothing was changed" not in e.message


@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_write_the_forge_failed_on_is_marked_as_possibly_landed(status):
    e = client.error_for(status, {"message": "bad gateway"}, write=True)
    assert e.may_have_landed and e.sent
    assert not client.error_for(status, {"message": "bad gateway"}).may_have_landed


@pytest.mark.parametrize("status", [401, 403, 404, 409, 422, 429])
def test_a_write_the_forge_refused_was_sent_and_did_not_land(status):
    e = client.error_for(status, {"message": "no"}, write=True)
    assert e.sent and not e.may_have_landed


def test_a_path_that_is_never_sent_is_not_sent():
    c = client.ForgejoClient("https://forge.example.test", BOT, transport=lambda *a: pytest.fail("sent"))
    with pytest.raises(client.ForgejoError) as e:
        c.post("/repos/a/../b/issues", {})
    assert e.value.kind == "bad_path" and not e.value.sent


STATUSES = (400, 401, 403, 404, 409, 422, 429, 500, 502, 503)


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("status", STATUSES)
def test_an_error_sentence_names_forgejo_and_tells_only_a_write_that_nothing_changed(status, write):
    message = client.error_for(status, {}, write=write).message
    assert "forge" not in message.lower().replace("forgejo", "")
    assert ("nothing was changed" in message) == (write and status == 429)
    assert all(len(sentence) <= 140 for sentence in message.split(". "))


def _access(url, *, timeout=None):
    """An access over real urllib; ``timeout`` shortens every request's wait."""
    values = {"url": url, "dry_run": False}
    transport = None if timeout is None else (
        lambda method, u, headers, body, _t: client.default_transport(method, u, headers, body, timeout))
    return access_mod.load_access(lambda k, d=None: values.get(k, d), {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get,
                                  transport=transport)


def _comment(url, *, timeout=None):
    """What Hermes does with forgejo_comment: the approval hook, then the handler."""
    tickets = approval.ApprovalTickets()
    gate = approval.make_gate(lambda: _access(url, timeout=timeout), lambda: ATTENDED, tickets)
    assert gate(tool_name="forgejo_comment", args=COMMENT, session_id="s")["action"] == "approve"
    return approval.run_write("forgejo_comment", COMMENT, "s", lambda: _access(url, timeout=timeout), ATTENDED, tickets)


def _is_comment(method, path):
    return method == "POST" and path.endswith("/issues/3/comments")


@pytest.mark.parametrize("fault,code", [("hang up", "no_answer"), ("cut", "no_answer"), ("stall", "timeout")])
def test_a_comment_the_forge_stored_is_answered_sent_and_may_have_landed(fault, code):
    with LoopbackForge(lambda m, p: fault if _is_comment(m, p) else None) as forge:
        out = _comment(forge.url, timeout=0.5 if fault == "stall" else None)
        stored = [w for w in forge.fake.writes if w[1].endswith("/issues/3/comments")]
    assert len(stored) == 1
    assert out["ok"] is False and out["sent"] is True and out["error"] == code and out["status"] is None
    assert "The write may have landed. Check before you repeat it." in out["message"]
    assert out["message"].endswith("forgejo_list kind=comments shows it.")
    assert "nothing was changed" not in out["message"]
    assert out["summary"] == 'a comment on PR #3 "Fix calc.mul"'


def test_a_comment_whose_connection_is_refused_is_answered_not_sent():
    write = sub("tools.write")
    with LoopbackForge() as forge:
        plan = write.plan(_access(forge.url), "forgejo_comment", COMMENT)
    out = write.execute(_access(f"http://127.0.0.1:{closed_port()}"), plan)
    assert out["ok"] is False and out["sent"] is False and out["error"] == "unreachable"
    assert "nothing was changed" in out["message"] and "may have landed" not in out["message"]
