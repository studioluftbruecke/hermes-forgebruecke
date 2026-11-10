"""Helpers for the read-tool tests: a forge with a second repo, and handlers built on it."""
from __future__ import annotations

import json

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, load_plugin, make_token, owner_account, repo, sample_data, sub

plugin = load_plugin()
approval = sub("approval")
BOT, OWNER = make_token("bot"), make_token("owner")
OTHER = "acme/backup"
BOT_ENV, OWNER_ENV = "FORGEBRUECKE_BOT_ACCOUNT_TOKEN", "FORGEBRUECKE_PERSONAL_WRITE_TOKEN"


class PagedReviews(FakeForgejo):
    """Pages ``/pulls/{n}/reviews`` as Forgejo does; the stock fake returns every row at once."""

    DEFAULT_PAGE = 30

    def _repo_read(self, full, r, rest, query, data, acct):
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "reviews":
            items = data.get("reviews", {}).get(int(rest[1]), [])
            limit, page = int(query.get("limit", self.DEFAULT_PAGE)), int(query.get("page", 1))
            return self._json(200, items[(page - 1) * limit: page * limit], {"x-total-count": str(len(items))})
        return super()._repo_read(full, r, rest, query, data, acct)


class ThreadForge(FakeForgejo):
    """Serves a review's line comments, and pages a thread's comments with ``page_cap`` rows at most per
    page, whatever limit is asked (0: the route does not page and returns every row)."""

    page_cap = 50

    def _repo_read(self, full, r, rest, query, data, acct):
        if len(rest) == 5 and rest[0] == "pulls" and rest[2] == "reviews" and rest[4] == "comments":
            return self._json(200, data.get("review_comments", {}).get(int(rest[3]), []))
        if len(rest) == 3 and rest[0] == "issues" and rest[2] == "comments":
            rows = data.get("comments", {}).get(int(rest[1]), [])
            if self.page_cap:
                limit, page = min(int(query.get("limit", 30)), self.page_cap), int(query.get("page", 1))
                rows_out = rows[(page - 1) * limit: page * limit]
            else:
                rows_out = rows
            return self._json(200, rows_out, {"x-total-count": str(len(rows))})
        return super()._repo_read(full, r, rest, query, data, acct)


class CappedAt30(ThreadForge):
    page_cap = 30


class NoPaging(ThreadForge):
    page_cap = 0


def kit(*, forge_cls=FakeForgejo, data=None, settings=None, extra_repos=0, tokens=("bot",)):
    """``(forge, get_setting, get_secret)``: the agent account sees DEMO (write) and OTHER (read)."""
    bot = bot_account(1 + extra_repos)
    bot["repos"] = bot["repos"] + [repo(OTHER, "read")]
    data = sample_data() if data is None else data
    for r in bot["repos"]:
        data.setdefault(r["full_name"], {"info": {"private": True, "default_branch": "main",
                                                  "html_url": f"{BASE}/{r['full_name']}"}})
    owner = {**owner_account(), "scopes": {"write:repository", "write:issue", "read:organization", "read:user"}}
    forge = forge_cls({BOT: bot, OWNER: owner}, data=data)
    values = {"url": BASE, **(settings or {})}
    env = {"bot": (BOT_ENV, BOT), "owner": (OWNER_ENV, OWNER)}
    secrets = dict(env[t] for t in tokens)
    return forge, (lambda k, d=None: values.get(k, d)), secrets.get


def read(fn, args, *, lane=None, **kw):
    """Call a read tool through its handler. Returns ``(result, forge)``."""
    forge, gs, sec = kit(**kw)
    handler = plugin.make_read_handler(fn, gs, sec, transport=forge, lane_fn=(lambda: lane) if lane else None)
    return json.loads(handler(args)), forge


def mention(repo_name: str = DEMO, number: int = 1):
    return approval.RunContext(platform="forgebruecke", chat_id=f"{repo_name}#{number}")


def paths(forge, suffix: str) -> list:
    return [c for c in forge.calls if c[1].endswith(suffix)]
