"""The desk's Refresh button reads the pull requests again, not the write token: the token's rights are learnt
by the doctor's scope probes (requests that are not reads), so they are kept for an hour per pair of tokens and
learnt again only when a token changes or the settings page is refreshed."""
from fb_fakes import BASE, sub
from test_fb4_desk_load import NOW, OWNER, BOT, YOU, Counting, forge, fresh_cache  # noqa: F401

desk = sub("desk")


def test_refresh_on_the_desk_sends_no_scope_probe(tmp_path):
    fake, _ = forge()
    t = Counting(fake)
    values = {"url": BASE, "dry_run": False, "you": YOU, "writes": [{"repo_glob": "acme/*", "flows": ["demo"]}]}
    secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT, "FORGEBRUECKE_PERSONAL_WRITE_TOKEN": OWNER}
    args = ((lambda k, d=None: values.get(k, d)), secrets.get)
    desk.api_desk(*args, data_dir=tmp_path, transport=t, now=lambda: NOW)
    assert any(method != "GET" for method, _ in t.kinds)  # the first read learnt the token's rights
    t.kinds.clear()
    for _ in range(3):
        status, body = desk.api_desk(*args, data_dir=tmp_path, transport=t, now=lambda: NOW, refresh=True)
        assert status == 200 and body["ok"]
    assert [kind for kind in t.kinds if kind[0] != "GET"] == []
