"""The client over a real socket and the real urllib: a write answered 2xx with a page that is not Forgejo's
may have landed, and an error body that echoes the request's headers never carries the token onward."""
import json

from fb_fakes import make_token, sub
from fb_fakes_rr_safety import ScriptedServer

client_mod = sub("client")
plugin = sub("__init__")


def test_a_write_answered_201_with_a_page_may_have_landed():
    token = make_token("h")
    with ScriptedServer((201, "text/html", "<html>ok</html>")) as server:
        client = client_mod.ForgejoClient(server.url, token, sleep=lambda s: None)
        try:
            client.post("/repos/acme/demo/issues/1/comments", {"body": "x"})
            raise AssertionError("a page is no Forgejo answer")
        except client_mod.ForgejoError as e:
            error = e
    assert len(server.seen) == 1 and error.may_have_landed and error.sent
    assert client_mod.MAY_HAVE_LANDED in error.message


def test_a_read_answered_200_with_a_page_is_still_not_forgejo():
    with ScriptedServer((200, "text/html", "<html>Sign in</html>")) as server:
        client = client_mod.ForgejoClient(server.url, make_token("h"), sleep=lambda s: None)
        try:
            client.get("/user")
        except client_mod.ForgejoError as e:
            assert e.kind == "not_forgejo" and not e.may_have_landed


def test_an_error_body_that_echoes_the_token_does_not_carry_it_on():
    token = make_token("echo")
    body = "Bad Request\nAuthorization: token {token}\nUser-Agent: forgebruecke"
    with ScriptedServer((400, "text/plain", body), (403, "application/json",
                                                     json.dumps({"message": "denied for token {token}"}))) as server:
        client = client_mod.ForgejoClient(server.url, token, sleep=lambda s: None)
        first = plugin._answer("probe", lambda: client.get("/user"))
        second = plugin._answer("probe", lambda: client.get("/user"))
    assert token in server.seen[0][2].get("Authorization", "")  # the premise: the server saw it
    assert token not in first and token not in second
    assert "Bad Request" in first
