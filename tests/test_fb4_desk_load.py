"""FB-4: the desk's load time. Its time went into Forgejo answers read one after another (44 reads, 11 s on the
live Forgejo; experiments/forgebruecke/desk-load-time). Independent reads now overlap, at most PARALLEL_READS at
once, and the number of reads one desk answer costs is pinned, so a change that adds reads fails here."""
import copy
import threading
import time

import pytest

from fb_fakes import BASE, FakeForgejo, make_token, repo, sub

desk = sub("desk")
client_mod = sub("client")
state_mod = sub("flows.state")
BOT, OWNER = make_token("bot"), make_token("owner")
REPOS = [f"acme/r{i}" for i in range(3)]
YOU = "acme"
NOW = 1794500000  # 2026-11-12: every age in two desk answers is the same


@pytest.fixture(autouse=True)
def fresh_cache():
    desk.clear_cache()
    yield
    desk.clear_cache()


class Counting:
    """A transport over the fake that waits ``delay`` per request and records the kind of each one and how many
    were in flight at once."""

    def __init__(self, inner, delay=0.0):
        self.inner, self.delay = inner, delay
        self.kinds: list = []
        self.live = self.peak = 0
        self.lock = threading.Lock()

    def __call__(self, method, url, headers, body, timeout):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        try:
            time.sleep(self.delay)
            return self.inner(method, url, headers, body, timeout)
        finally:
            path = url.split("/api/v1", 1)[-1].split("?", 1)[0].split("/")
            kind = ("comments" if path[-1] == "comments" else "reviews" if path[-1] == "reviews" else
                    "status" if path[-1] == "status" else "search" if path[-1] == "search" else
                    "pulls" if path[-1] == "pulls" else "branch" if "branches" in path else "other")
            with self.lock:
                self.live -= 1
                self.kinds.append((method, kind))

    def count(self) -> dict:
        out: dict = {}
        for method, kind in self.kinds:
            out[f"{method} {kind}"] = out.get(f"{method} {kind}", 0) + 1
        return out


def forge() -> "tuple[FakeForgejo, list]":
    """Three repos, two open pull requests each. Even ones ask acme to review, odd ones do not (their reviews are
    read); each has two comments and a status. The first carries a flow whose merge gate waits for acme."""
    data, states = {}, []
    for k in range(6):
        name, n, sha = REPOS[k % 3], 10 + k, f"{k:040x}"
        url = f"{BASE}/{name}/pulls/{n}"
        d = data.setdefault(name, {"info": {"default_branch": "main", "html_url": f"{BASE}/{name}"}, "issues": {},
                                   "pulls": {}, "comments": {}, "reviews": {}, "status": {},
                                   "branches": {"main": {"name": "main", "commit": {"id": "1" * 40},
                                                         "user_can_merge_by": {YOU: True}}}})
        issue = {"number": n, "title": f"Change {k}", "state": "open", "user": {"id": 7, "login": "hermes"},
                 "labels": [], "pull_request": {"merged": False, "draft": False, "html_url": url}, "html_url": url,
                 "repository": {"full_name": name}, "updated_at": "2026-11-12T10:00:00Z"}
        d["issues"][n] = issue
        d["pulls"][n] = {**issue, "draft": False, "merged": False, "mergeable": True, "base": {"ref": "main"},
                         "head": {"ref": f"c{k}", "sha": sha},
                         "requested_reviewers": [{"id": 1, "login": YOU}] if k % 2 == 0 else []}
        d["comments"][n] = [{"id": 100 * k + j, "user": {"id": 9, "login": "dev"}, "body": f"note {j}",
                             "created_at": f"2026-11-12T09:0{j}:00Z"} for j in range(2)]
        d["status"][sha] = {"state": "success", "sha": sha, "total_count": 1,
                            "statuses": [{"context": "ci", "status": "success"}]}
        if k == 0:
            states.append({"repo": name, "pr": n, "flow": "demo", "head_sha": sha, "title": f"Change {k}", "url": url,
                           "updated_at": 100, "base": "main",
                           "gates": [{"gate_key": "human", "type": "human", "state": "waiting",
                                      "detail": f"waiting for {YOU} to merge in Forgejo"}]})
    bot = {"user": {"id": 4, "login": "forgebruecke-bot", "is_admin": False},
           "scopes": {"read:notification", "write:issue", "write:repository", "read:user"},
           "repos": [repo(n, "write") for n in REPOS]}
    owner = {"user": {"id": 1, "login": YOU, "is_admin": True},
             "scopes": {"write:repository", "write:issue", "read:organization", "read:user"},
             "repos": [repo(n, "admin") for n in REPOS]}
    return FakeForgejo({BOT: bot, OWNER: owner}, data=data), states


def poll(tmp_path, delay=0.0):
    """One desk read as the page's poll meets it: every cache warm except the desk's own 15 s answer. A flow store
    already in ``tmp_path`` is kept (``save_pr`` stamps the wall clock, so two desks compare only on one store)."""
    fake, states = forge()
    store = state_mod.FlowStore(tmp_path)
    for st in states if not store.all_prs() else ():
        store.save_pr(st)
    t = Counting(fake, delay)
    values = {"url": BASE, "dry_run": False, "you": YOU, "writes": [{"repo_glob": "acme/*", "flows": ["demo"]}]}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}
    args = ((lambda k, d=None: values.get(k, d)), secrets.get)
    desk.api_desk(*args, data_dir=tmp_path, transport=t, now=lambda: NOW)
    desk.clear_desk_cache()
    t.kinds.clear()
    t.peak = 0
    status, body = desk.api_desk(*args, data_dir=tmp_path, transport=t, now=lambda: NOW)
    assert status == 200 and body["ok"]
    return t, body


def test_one_desk_read_costs_a_fixed_number_of_forgejo_requests(tmp_path):
    t, body = poll(tmp_path)
    assert len(body["rows"]) == 6 and all(r["can_merge"] for r in body["rows"])
    # 1 search, 1 pulls list per repo, per row 1 comments page and 1 status, reviews for the 3 rows that do not
    # ask acme (the flow row's reviews are never read; it is one of the even ones). No write, no merge-right read
    # (kept 5 minutes), no repo list (kept with the rights).
    assert t.count() == {"GET search": 1, "GET pulls": 3, "GET comments": 6, "GET status": 6, "GET reviews": 3}


def test_the_desk_overlaps_its_reads_and_never_more_than_the_limit(tmp_path):
    t, body = poll(tmp_path, delay=0.02)
    assert 1 < t.peak <= client_mod.PARALLEL_READS
    assert len(body["rows"]) == 6


def test_overlapped_reads_build_the_same_desk_as_one_at_a_time(tmp_path, monkeypatch):
    def rows(body):
        return [{k: v for k, v in r.items() if k != "age_s"} for r in body["rows"]]

    _, parallel = poll(tmp_path, delay=0.005)
    desk.clear_cache()
    monkeypatch.setattr(client_mod, "PARALLEL_READS", 1)
    t, serial = poll(tmp_path)
    assert t.peak == 1
    differ = {(a["id"], k): (a[k], b.get(k)) for a, b in zip(rows(parallel), rows(serial)) for k in a if a[k] != b.get(k)}
    assert differ == {}
    assert len(parallel["rows"]) == len(serial["rows"]) == 6 and parallel["counts"] == serial["counts"]


def test_parallel_map_keeps_the_order_and_raises_the_first_error():
    assert client_mod.parallel_map(lambda x: x * 2, range(20), 4) == [x * 2 for x in range(20)]

    def boom(x):
        if x in (3, 7):
            raise ValueError(x)
        return x

    with pytest.raises(ValueError) as caught:
        client_mod.parallel_map(boom, range(10), 4)
    assert caught.value.args == (3,)


def test_parallel_map_starts_no_call_after_an_error():
    """R149-3: a 429 (or any error) on one read must not send the reads still queued. Only the calls already
    running when it failed finish. Each call takes 50 ms; item 5 fails at once, so items 0-7 have started by then
    and 8-19 never start (before the fix: up to item 12, since the queue ran on until the result of item 4)."""
    started, lock = set(), threading.Lock()

    def read(x):
        with lock:
            started.add(x)
        if x == 5:
            raise client_mod.ForgejoError(429, "rate_limited", "Forgejo asked to slow down.")
        time.sleep(0.05)
        return x

    with pytest.raises(client_mod.ForgejoError) as caught:
        client_mod.parallel_map(read, range(20), 4)
    assert caught.value.status == 429
    time.sleep(0.15)  # a straggler would have started by now
    assert set(range(6)) <= started and max(started) <= 7, sorted(started)


def test_a_repo_expand_reads_the_checks_side_by_side(tmp_path):
    fake, _ = forge()
    many = copy.deepcopy(fake.data[REPOS[0]])
    for k in range(6, 20):
        n, sha = 10 + k, f"{k:040x}"
        many["pulls"][n] = {**many["pulls"][10], "number": n, "head": {"ref": f"c{k}", "sha": sha}}
        many["status"][sha] = many["status"][f"{0:040x}"]
    fake.data[REPOS[0]] = many
    t = Counting(fake, 0.02)
    values = {"url": BASE, "dry_run": False, "you": YOU, "writes": []}
    status, body = desk.api_repo_pulls(lambda k, d=None: values.get(k, d),
                                       {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get, REPOS[0], data_dir=tmp_path,
                                       transport=t)
    assert status == 200 and len(body["pulls"]) == 16 and all(p["checks"] for p in body["pulls"])
    assert 1 < t.peak <= client_mod.PARALLEL_READS
