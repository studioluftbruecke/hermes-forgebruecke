"""ForgejoClient: retries only for a GET on 5xx and 429, pagination, error sentences, version gate, caching."""
import http.server
import threading

import pytest

from fb_fakes import BASE, SWAGGER, Scripted, make_token, sub

client = sub("client")


def make(transport, **kw):
    sleeps = []
    c = client.ForgejoClient(BASE, make_token("t"), transport=transport, sleep=sleeps.append, **kw)
    return c, sleeps


def test_retries_5xx_with_backoff_then_succeeds():
    t = Scripted((500, {"message": "boom"}), (502, b"bad gateway"), (200, {"ok": 1}))
    c, sleeps = make(t)
    assert c.get("/user").data == {"ok": 1}
    assert len(t.calls) == 3
    assert sleeps == [0.5, 1.0]


def test_429_honours_retry_after():
    t = Scripted((429, {"message": "slow down"}, {"Retry-After": "2"}), (200, []))
    c, sleeps = make(t)
    c.get("/notifications")
    assert sleeps == [2.0] and len(t.calls) == 2


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "soon"])
def test_a_retry_after_that_is_no_finite_number_falls_back_to_the_backoff(value):
    t = Scripted((429, {"message": "slow down"}, {"Retry-After": value}), (200, []))
    c, sleeps = make(t)
    c.get("/notifications")
    assert sleeps == [0.5] and len(t.calls) == 2


def test_a_read_gives_up_after_three_retries_and_does_not_speak_of_changes():
    t = Scripted(*[(503, {"message": "down"})] * 4)
    c, sleeps = make(t)
    with pytest.raises(client.ForgejoError) as e:
        c.get("/repos/a/b/issues/1/comments")
    assert len(t.calls) == 4 and len(sleeps) == 3
    assert e.value.kind == "unreachable" and e.value.message == "Forgejo is unreachable or failing (503)."


def test_retries_can_be_lowered_after_construction():
    t = Scripted(*[(503, {"message": "down"})] * 3)
    c, sleeps = make(t)
    assert c.retries == 3
    c.retries = 0
    with pytest.raises(client.ForgejoError):
        c.get("/user")
    assert len(t.calls) == 1 and sleeps == []
    c.retries = 1
    with pytest.raises(client.ForgejoError):
        c.get("/user")
    assert len(t.calls) == 3 and sleeps == [0.5]


@pytest.mark.parametrize("send", [
    lambda c: c.post("/repos/a/b/issues/1/comments", {"body": "x"}),
    lambda c: c.patch("/repos/a/b/issues/1", {"state": "closed"}),
    lambda c: c.request("put", "/repos/a/b/contents/x", json_body={"content": ""}),
])
@pytest.mark.parametrize("status", [500, 502, 504])
def test_a_write_the_forge_failed_on_is_sent_once_and_may_have_landed(send, status):
    t = Scripted((status, {"message": "bad gateway"}), (201, {"id": 1}))
    c, sleeps = make(t)
    with pytest.raises(client.ForgejoError) as e:
        send(c)
    assert len(t.calls) == 1 and sleeps == []
    assert e.value.kind == "unreachable" and e.value.status == status
    assert "may have landed" in e.value.message and "nothing was changed" not in e.value.message


def test_a_rate_limited_write_is_sent_once():
    t = Scripted((429, {"message": "slow down"}, {"Retry-After": "2"}), (201, {"id": 1}))
    c, sleeps = make(t)
    with pytest.raises(client.ForgejoError) as e:
        c.post("/repos/a/b/issues/1/comments", {"body": "x"})
    assert len(t.calls) == 1 and sleeps == [] and e.value.kind == "rate_limited"


@pytest.mark.parametrize("status,kind", [(400, "http_error"), (401, "token_rejected"), (403, "forbidden"),
                                         (404, "not_found"), (409, "rejected"), (422, "rejected")])
def test_4xx_is_never_retried(status, kind):
    t = Scripted((status, {"message": "nope"}))
    c, sleeps = make(t)
    with pytest.raises(client.ForgejoError) as e:
        c.get("/user")
    assert len(t.calls) == 1 and sleeps == []
    assert e.value.kind == kind and e.value.status == status


def test_403_for_a_missing_scope_names_the_scope():
    t = Scripted((403, {"message": "token does not have at least one of required scope(s): [read:user read:organization]"}))
    c, _ = make(t)
    with pytest.raises(client.ForgejoError) as e:
        c.get("/user/orgs")
    assert e.value.kind == "missing_scope"
    assert e.value.scopes == ("read:user", "read:organization")
    assert "read:organization" in e.value.message


def test_422_is_passed_through_verbatim():
    c, _ = make(Scripted((422, {"message": "[Title]: Required"})))
    with pytest.raises(client.ForgejoError) as e:
        c.post("/repos/a/b/issues", {})
    assert e.value.message == "[Title]: Required"


def test_unreachable_forge_is_not_retried_and_says_so():
    calls = []

    def down(*a):
        calls.append(a)
        raise client.ForgejoError(None, "unreachable", "Forgejo is unreachable (Connection refused).")

    c, _ = make(down)
    with pytest.raises(client.ForgejoError) as e:
        c.get("/version")
    assert len(calls) == 1 and e.value.message == "Forgejo is unreachable (Connection refused)."


def _pages(n_items, *, total_header=True, limit=50):
    items = [{"id": i} for i in range(n_items)]
    answers = []
    for start in range(0, n_items + 1, limit):
        chunk = items[start:start + limit]
        headers = {"X-Total-Count": str(n_items)} if total_header else {}
        answers.append((200, chunk, headers))
        if len(chunk) < limit:
            break
    return answers


def test_pagination_follows_pages_and_total():
    t = Scripted(*_pages(120))
    c, _ = make(t)
    page = c.paginate("/user/repos")
    assert len(page.items) == 120 and page.total == 120 and page.has_more is False
    assert [q[1].split("page=")[1].split("&")[0] for q in t.calls] == ["1", "2", "3"]


def test_pagination_is_honest_when_capped():
    t = Scripted(*_pages(120))
    c, _ = make(t)
    page = c.paginate("/user/repos", max_items=60)
    assert len(page.items) == 60 and page.total == 120 and page.has_more is True


def test_pagination_without_total_header():
    c, _ = make(Scripted(*_pages(70, total_header=False)))
    page = c.paginate("/x")
    assert len(page.items) == 70 and page.total is None and page.has_more is False
    c2, _ = make(Scripted(*_pages(120, total_header=False)))
    page2 = c2.paginate("/x", max_items=100)
    assert len(page2.items) == 100 and page2.has_more is True


def _ids(start, stop):
    return [{"id": i} for i in range(start, stop)]


def test_pagination_goes_on_when_the_forge_caps_pages_below_the_limit():
    total = {"X-Total-Count": "90"}
    t = Scripted((200, _ids(0, 30), total), (200, _ids(30, 60), total), (200, _ids(60, 90), total))
    c, _ = make(t)
    page = c.paginate("/user/repos", max_items=500)
    assert page.items == _ids(0, 90) and page.total == 90 and page.has_more is False
    assert len(t.calls) == 3


def test_pagination_ends_at_an_empty_page_even_when_the_total_says_more():
    total = {"X-Total-Count": "90"}
    t = Scripted((200, _ids(0, 30), total), (200, _ids(30, 60), total), (200, [], total))
    c, _ = make(t)
    page = c.paginate("/user/repos", max_items=500)
    assert page.items == _ids(0, 60) and page.has_more is False and len(t.calls) == 3


def test_pagination_without_a_total_asks_for_page_two_after_a_short_first_page():
    t = Scripted((200, _ids(0, 30)), (200, []))
    c, _ = make(t)
    page = c.paginate("/repos/a/b/assignees", max_items=500)
    assert page.items == _ids(0, 30) and page.total is None and page.has_more is False
    assert [q[1].split("page=")[1].split("&")[0] for q in t.calls] == ["1", "2"]


def test_pagination_without_a_total_follows_a_capped_forge_to_its_last_page():
    t = Scripted((200, _ids(0, 30)), (200, _ids(30, 60)), (200, _ids(60, 70)))
    c, _ = make(t)
    page = c.paginate("/repos/a/b/assignees", max_items=500)
    assert page.items == _ids(0, 70) and page.has_more is False and len(t.calls) == 3


def test_pagination_does_not_repeat_a_list_from_an_endpoint_that_ignores_the_page():
    t = Scripted(*[(200, _ids(0, 30))] * 20)
    c, _ = make(t)
    page = c.paginate("/repos/a/b/assignees", max_items=500)
    assert page.items == _ids(0, 30) and page.has_more is False and len(t.calls) == 2


@pytest.mark.parametrize("raw,product,supported", [
    ("15.0.11+gitea-1.22.0", "forgejo", True),
    ("11.0.3+gitea-1.22.0", "forgejo", True),
    ("6.0.1+gitea-1.21.0", "forgejo", False),
    ("1.22.3", "gitea", True),
    ("1.19.0", "gitea", False),
    ("nonsense", "unknown", False),
])
def test_version_gate(raw, product, supported):
    p, _, ok, reason = client.parse_version(raw)
    assert (p, ok) == (product, supported)
    assert reason


def test_capabilities_are_cached_for_ten_minutes():
    now = [1000.0]
    t = Scripted((200, {"version": "15.0.11+gitea-1.22.0"}), (200, SWAGGER),
                 (200, {"version": "15.0.12+gitea-1.22.0"}), (200, SWAGGER))
    c, _ = make(t, clock=lambda: now[0])
    caps = c.capabilities()
    assert caps.supported and caps.has("GET", "/notifications") and not caps.has("DELETE", "/notifications")
    assert c.capabilities() is caps and len(t.calls) == 2
    now[0] += 601
    assert c.capabilities().raw_version.startswith("15.0.12") and len(t.calls) == 4
    assert t.calls[1][1] == f"{BASE}/swagger.v1.json"


def test_token_goes_in_the_header_and_never_in_repr():
    tok = make_token("secret")
    t = Scripted((200, {}))
    c = client.ForgejoClient(BASE + "/api/v1/", tok, transport=t)
    c.get("/user")
    assert t.calls[0][2]["Authorization"] == f"token {tok}"
    assert t.calls[0][1] == f"{BASE}/api/v1/user"
    assert tok not in repr(c)


def test_bad_url_is_refused_with_a_sentence():
    with pytest.raises(client.ForgejoError) as e:
        client.ForgejoClient("forge.example.test", "x")
    assert e.value.kind == "bad_url" and "https://" in e.value.message


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_a_redirect_is_an_error_that_names_the_target(method, status):
    t = Scripted((status, b"", {"Location": "https://other.example.test/api/v1/user"}), (200, {"ok": 1}))
    c, sleeps = make(t)
    with pytest.raises(client.ForgejoError) as e:
        c.request(method, "/user", json_body=None if method == "GET" else {"body": "x"})
    assert len(t.calls) == 1 and sleeps == []
    assert e.value.kind == "redirect" and e.value.status == status
    assert "https://other.example.test/api/v1/user" in e.value.message and "address" in e.value.message


@pytest.mark.parametrize("path", [
    "/repos/acme/demo/releases/tags/../../../../acme/backup/issues/1",
    "/repos/../user",
    "/repos/acme/..",
    "/repos/./.",
    "/repos/acme/%2e%2e/backup",
    "/repos/acme/%2E/backup",
])
def test_a_path_with_a_dot_segment_is_refused_before_any_request(path):
    t = Scripted((200, {}))
    c, _ = make(t)
    for send in (lambda: c.url(path), lambda: c.get(path), lambda: c.patch(path, {"x": 1})):
        with pytest.raises(client.ForgejoError) as e:
            send()
        assert e.value.kind == "bad_path"
    assert t.calls == []


def test_dots_inside_a_segment_are_not_dot_segments():
    c, _ = make(Scripted())
    path = "/repos/acme/my.repo/contents/.github/v1..v2/..hidden"
    assert c.url(path, {"ref": ".."}) == f"{BASE}/api/v1{path}?ref=.."


def test_a_success_that_is_not_json_says_the_address_is_not_forgejo():
    page = b"<html><body>Sign in</body></html>"
    c, sleeps = make(Scripted((200, page), (200, page)))
    with pytest.raises(client.ForgejoError) as e:
        c.get("/repos/a/b")
    assert e.value.kind == "not_forgejo" and e.value.status == 200 and "Forgejo address" in e.value.message
    with pytest.raises(client.ForgejoError) as e:
        c.capabilities()
    assert e.value.kind == "not_forgejo" and sleeps == []


def test_raw_bodies_empty_bodies_and_failed_pages_keep_their_meaning():
    c, _ = make(Scripted((200, b"diff --git a/x b/x"), (204, b""), (404, b"<html>gone</html>")))
    assert c.get("/repos/a/b/pulls/1.diff", raw=True).data == b"diff --git a/x b/x"
    assert c.post("/repos/a/b/issues/1/labels", {}).data is None
    with pytest.raises(client.ForgejoError) as e:
        c.get("/repos/a/b")
    assert e.value.kind == "not_found"


class _RedirectingForge(http.server.BaseHTTPRequestHandler):
    """Answers 302 to /landed for every path but /landed, and records each request it gets."""

    def _answer(self):
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization")))
        if self.path == "/landed":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()

    do_GET = do_POST = _answer

    def log_message(self, *args):
        pass


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_the_default_transport_does_not_follow_a_redirect(method, monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    server = http.server.HTTPServer(("127.0.0.1", 0), _RedirectingForge)
    server.seen = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        c = client.ForgejoClient(f"http://127.0.0.1:{server.server_port}", make_token("t"),
                                 sleep=lambda _s: None, clock=lambda: 0.0, timeout=5)
        with pytest.raises(client.ForgejoError) as e:
            c.request(method, "/user", json_body=None if method == "GET" else {"body": "x"})
    finally:
        server.shutdown()
        server.server_close()
    assert e.value.kind == "redirect" and "/landed" in e.value.message
    assert [(m, path) for m, path, _auth in server.seen] == [(method, "/api/v1/user")]
