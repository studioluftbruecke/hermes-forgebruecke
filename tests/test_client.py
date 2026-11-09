"""ForgejoClient: retries only on 5xx and 429, pagination, error sentences, version gate, caching."""
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


def test_gives_up_after_three_retries_and_says_nothing_changed():
    t = Scripted(*[(503, {"message": "down"})] * 4)
    c, sleeps = make(t)
    with pytest.raises(client.ForgejoError) as e:
        c.post("/repos/a/b/issues/1/comments", {"body": "x"})
    assert len(t.calls) == 4 and len(sleeps) == 3
    assert e.value.kind == "unreachable" and "nothing was changed" in e.value.message


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
        raise client.ForgejoError(None, "unreachable", "The forge is unreachable (refused); nothing was changed.")

    c, _ = make(down)
    with pytest.raises(client.ForgejoError) as e:
        c.get("/version")
    assert len(calls) == 1 and "nothing was changed" in e.value.message


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
