"""Kits for the tools-write tests: a fake forge with a second repo, the three write tools behind the real
approval gate, and the flows tool called the way the plugin's handler calls it."""
from __future__ import annotations

import json

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, repo, sample_data, sub

approval = sub("approval")
access_mod = sub("access")
flows_tool = sub("tools.flows")
state_mod = sub("flows.state")
engine_mod = sub("flows.engine")

BOT = make_token("bot")
OTHER = "acme/backup"
FLOW = {"flow": "demo", "repos": [DEMO], "gates": [{"review": {"by": "forgebruecke-dev"}}, {"human": {"who": "acme"}}]}
ATTENDED = approval.RunContext()
MENTION = approval.RunContext(platform="forgebruecke", chat_id=f"{DEMO}#1")


class FakeRunner:
    def __init__(self):
        self.started: list = []

    def start(self, *, profile, prompt_path, log_path):
        self.started.append(profile)
        return 1000 + len(self.started)

    def alive(self, pid):
        return True

    def stop(self, pid):
        pass


class Kit:
    """``other`` is the agent account's right on OTHER; ``extra_repos`` adds that many more write repos."""

    def __init__(self, *, settings=None, other="read", extra_repos=0, data=None, forge_cls=FakeForgejo):
        bot = bot_account(1 + extra_repos)
        bot["repos"] = bot["repos"] + [repo(OTHER, other)]
        data = sample_data() if data is None else data
        data[DEMO]["pulls"][3]["state"] = "open"
        for r in bot["repos"]:
            data.setdefault(r["full_name"], {"info": {"private": True, "default_branch": "main",
                                                      "html_url": f"{BASE}/{r['full_name']}"}})
        self.bot = bot
        self.forge = forge_cls({BOT: bot}, data=data)
        self.values = {"url": BASE, "dry_run": False, "writes": [{"repo_glob": DEMO, "flows": ["demo"]}],
                       **(settings or {})}
        self.secrets = {"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def load(self, run=None):
        lane, scope = run.lane() if run is not None else ("agent", "")
        return access_mod.load_access(self.get_setting, self.secrets.get, transport=self.forge, lane=lane,
                                      scope_repo=scope)

    def write(self, tool, args, *, run=ATTENDED, spec=None):
        """What Hermes does with a gated call: the hook, then the handler. Returns ``(verdict, result)``."""
        tickets = approval.ApprovalTickets(clock=lambda: 0.0)
        gate = approval.make_gate(self.load, lambda: run, tickets, extra={tool: spec} if spec else None)
        verdict = gate(tool_name=tool, args=args, session_id="s")
        if verdict and verdict["action"] == "block":
            return verdict, None
        return verdict, approval.run_write(tool, args, "s", self.load, run, tickets, spec=spec)


class FlowsKit(Kit):
    def __init__(self, tmp_path, **kw):
        super().__init__(**kw)
        self.store = state_mod.FlowStore(tmp_path)
        self.store.write_override("demo", json.dumps(FLOW))
        self.runner = FakeRunner()

    def call(self, args, run=None, *, pass_run=True):
        """The flows tool's ungated actions in ``run``'s lane; a refusal comes back as the handler returns it."""
        extra = {"run": run} if run is not None and pass_run else {}
        try:
            return flows_tool.handle(self.load(run), self.store, args, **extra,
                                     engine_factory=lambda a, s: engine_mod.Engine(a, s, runner=self.runner,
                                                                                   now=lambda: 1_800_000_000.0))
        except access_mod.Refusal as r:
            return r.to_dict()
