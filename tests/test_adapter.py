"""Mention channel (adapter half): the real adapter methods against the fake forge, with stand-ins for the
gateway modules it imports."""
import asyncio
import json
import sys
import types

import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, sample_data, sub
from fb_fakes_workers import Faulty, gateway_stubs

poller = sub("channel.poller")
settings_mod = sub("settings")
client_mod = sub("client")
BOT = make_token("bot")
T0 = poller.parse_ts("2026-11-08T13:00:00Z")


@pytest.fixture
def adapter_mod(monkeypatch):
    for name, module in gateway_stubs().items():
        monkeypatch.setitem(sys.modules, name, module)
    module = sub("channel.adapter")
    yield module
    sys.modules.pop(module.__name__, None)


def comment(cid, body, at, author="hermes"):
    return {"id": cid, "user": {"login": author}, "body": body, "created_at": at, "updated_at": at,
            "html_url": f"{BASE}/{DEMO}/issues/1#issuecomment-{cid}"}


class Channel:
    """One adapter on a forge with two mentions in issue 1, a ledger on disk, and a clock the test moves."""

    def __init__(self, adapter_mod, tmp_path, *, dry_run=False, comments=None):
        data = sample_data()
        data[DEMO]["perms"] = {"hermes": "write"}
        data[DEMO]["comments"][1] = comments if comments is not None else [
            comment(900, "@forgebruecke-bot first", "2026-11-08T13:00:10Z"),
            comment(901, "@forgebruecke-bot second", "2026-11-08T13:00:20Z")]
        self.forge = FakeForgejo({BOT: bot_account()}, data=data)
        self.forge.notifications = [{"id": 1, "updated_at": "2026-11-08T13:00:30Z", "repository": {"full_name": DEMO},
                                     "subject": {"type": "Issue", "url": f"{BASE}/api/v1/repos/{DEMO}/issues/1"}}]
        self.transport = Faulty(self.forge)
        self.values = {"url": BASE, "writes": [], "dry_run": dry_run}
        self.now = T0 + 60
        self.delivered: list = []
        self.refuse: set = set()
        self.adapter = adapter_mod.make_adapter(types.SimpleNamespace(extra={}), home=tmp_path / "home",
                                                data_dir=tmp_path / "state",
                                                get_secret={"FORGEBRUECKE_BOT_ACCOUNT_TOKEN": BOT}.get)
        self.adapter._client = client_mod.ForgejoClient(BASE, BOT, transport=self.transport,
                                                        sleep=lambda s: None, clock=lambda: 0.0)
        self.adapter._bot = "forgebruecke-bot"
        self.adapter._now = lambda: self.now
        self.adapter._settings = lambda config=None: self.settings()
        self.adapter.handle_message = self._handle
        poller.Ledger({"since": "2026-11-08T13:00:00Z"}).save(self.adapter.ledger_path)

    async def _handle(self, event):
        self.delivered.append(event.message_id)
        if event.message_id in self.refuse:
            raise RuntimeError("the gateway refused this event")

    def settings(self):
        return settings_mod.load_settings(lambda k, d=None: self.values.get(k, d))

    def poll(self, after=60):
        self.now += after
        asyncio.run(self.adapter._poll_once(self.settings()))

    def restart(self, adapter_mod):
        """A new gateway process: a new adapter over the same ledger on disk."""
        old = self.adapter
        self.adapter = adapter_mod.make_adapter(types.SimpleNamespace(extra={}), home=old.home, data_dir=old.data_dir,
                                                get_secret=lambda name: None)
        self.adapter._client, self.adapter._bot, self.adapter._now = old._client, old._bot, old._now
        self.adapter._settings, self.adapter.handle_message = old._settings, old.handle_message

    def answered(self):
        return sorted(poller.Ledger.load(self.adapter.ledger_path).answered)

    def events(self, name):
        lines = self.adapter.events_path.read_text(encoding="utf-8").splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == name]

    def reactions(self):
        return [p for m, p, b in self.forge.writes if p.endswith("/reactions")]

    def comments(self):
        return [p for m, p, b in self.forge.writes if p.endswith("/comments")]


def test_one_failing_delivery_keeps_the_others_and_is_tried_again(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.refuse = {"900"}
    ch.poll()
    assert ch.delivered == ["900", "901"]
    assert ch.answered() == [f"{DEMO}#1/c901"]  # 900 was not delivered, so the ledger does not say it was
    assert [e["key"] for e in ch.events("deliver_failed")] == [f"{DEMO}#1/c900"]
    ch.refuse = set()
    ch.poll(after=600)  # well past the overlap: the cursor waited for the failed one
    assert ch.delivered == ["900", "901", "900"]
    assert ch.answered() == [f"{DEMO}#1/c900", f"{DEMO}#1/c901"]
    ch.poll()
    assert ch.delivered == ["900", "901", "900"]


def test_a_failed_delivery_is_tried_again_long_after_the_overlap_ran_out(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    ch.forge.data[DEMO]["comments"][1].append(comment(902, "@forgebruecke-bot third", "2026-11-08T13:30:00Z"))
    ch.forge.notifications[0]["updated_at"] = "2026-11-08T13:30:00Z"
    ch.refuse = {"902"}
    ch.poll(after=1800)
    ch.refuse = set()
    ch.poll(after=1800)
    assert ch.delivered == ["900", "901", "902", "902"] and f"{DEMO}#1/c902" in ch.answered()


def test_a_poll_cut_off_between_two_deliveries_loses_neither(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    original = ch.adapter.handle_message

    async def cut_off_at_the_second(event):
        if event.message_id == "901":
            raise asyncio.CancelledError()  # the gateway stops while the adapter delivers
        await original(event)
    ch.adapter.handle_message = cut_off_at_the_second
    with pytest.raises(asyncio.CancelledError):
        ch.poll()
    assert ch.answered() == [f"{DEMO}#1/c900"]
    ch.adapter.handle_message = original
    ch.poll(after=1800)
    assert ch.delivered == ["900", "901"]


class Died(BaseException):
    """The gateway process ended here. Not an ``Exception``, so nothing in the adapter catches it."""


def save_fails_once_after_the_hand_off(monkeypatch, error):
    save, left = poller.Ledger.save, [1]

    def failing(self, path):
        if self.answered and left:
            left.pop()
            raise error
        save(self, path)
    monkeypatch.setattr(poller.Ledger, "save", failing)


def test_a_ledger_save_that_failed_after_the_hand_off_does_not_hand_the_mention_over_again(adapter_mod, tmp_path,
                                                                                         monkeypatch):
    ch = Channel(adapter_mod, tmp_path, comments=[comment(900, "@forgebruecke-bot first", "2026-11-08T13:00:10Z")])
    save_fails_once_after_the_hand_off(monkeypatch, OSError("No space left on device"))
    with pytest.raises(OSError):
        ch.poll()
    assert ch.delivered == ["900"] and ch.answered() == []
    ch.poll()  # the same process: it knows what it handed over, and only the save is done again
    assert ch.delivered == ["900"] and ch.answered() == [f"{DEMO}#1/c900"]
    ch.poll()
    assert ch.delivered == ["900"]


def test_a_gateway_that_died_between_the_hand_off_and_the_save_hands_the_mention_over_again(adapter_mod, tmp_path,
                                                                                          monkeypatch):
    ch = Channel(adapter_mod, tmp_path, comments=[comment(900, "@forgebruecke-bot first", "2026-11-08T13:00:10Z")])
    save_fails_once_after_the_hand_off(monkeypatch, Died())
    with pytest.raises(Died):
        ch.poll()
    ch.restart(adapter_mod)
    ch.poll()  # at least once: the new process cannot know of the hand-off, and losing the mention is worse
    assert ch.delivered == ["900", "900"] and ch.answered() == [f"{DEMO}#1/c900"]
    ch.poll()
    assert ch.delivered == ["900", "900"]


def test_a_poll_starts_from_the_ledger_on_disk(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll(after=0)
    ch.forge.data[DEMO]["comments"][1].append(comment(902, "@forgebruecke-bot third", "2026-11-08T13:00:25Z"))
    newer_adapter = poller.Ledger.load(ch.adapter.ledger_path)  # another adapter answered 902 meanwhile
    newer_adapter.mark(f"{DEMO}#1/c902", f"{DEMO}#1", ch.now)
    newer_adapter.save(ch.adapter.ledger_path)
    ch.poll(after=30)
    assert ch.delivered == ["900", "901"]
    assert ch.answered() == [f"{DEMO}#1/c900", f"{DEMO}#1/c901", f"{DEMO}#1/c902"]


def long_answer():
    return "\n\n".join("x" * 1000 for _ in range(poller.REPLY_LIMIT // 1000 + 30))


def failing_post(ch, which):
    """The ``which``-th comment POST of the channel times out; the others reach the forge."""
    posts = []

    def respond(method, url, headers, body, timeout, inner):
        posts.append(url)
        if len(posts) == which:
            raise client_mod.ForgejoError(None, "timeout", "The forge did not answer in time.")
        return inner(method, url, headers, body, timeout)
    ch.transport.answer(lambda m, p: m == "POST" and p.endswith("/issues/1/comments"), respond)


def test_an_answer_cut_off_after_its_first_part_is_reported_as_partly_delivered(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    failing_post(ch, 2)
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", long_answer()))
    # Hermes sends a failed answer again as plain text unless the result says a part is already visible
    assert not result.success and result.raw_response == {"partial_overflow": True}
    assert len([p for m, p, b in ch.forge.writes if p.endswith("/issues/1/comments")]) == 1


def test_an_answer_whose_first_part_fails_is_a_plain_failure(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    failing_post(ch, 1)
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", long_answer()))
    assert not result.success and result.raw_response is None and ch.comments() == []


def test_only_a_poll_that_found_or_skipped_something_leaves_a_poll_line(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path, comments=[comment(900, "@forgebruecke-bot hi", "2026-11-08T13:00:10Z"),
                                                  comment(901, "@forgebruecke-bot me too", "2026-11-08T13:00:20Z",
                                                          author="stranger")])
    ch.poll()
    assert [(e["found"], len(e["skipped"])) for e in ch.events("poll")] == [(1, 1)]
    ch.poll(after=600)  # the poll after the cursor moved reads the overlap again and skips the same comment
    assert [(e["found"], len(e["skipped"])) for e in ch.events("poll")] == [(1, 1), (0, 1)]
    lines = len(ch.adapter.events_path.read_text(encoding="utf-8").splitlines())
    for _ in range(5):
        ch.poll(after=600)  # nothing new on the forge
    assert len(ch.adapter.events_path.read_text(encoding="utf-8").splitlines()) == lines


def test_a_live_poll_reacts_and_delivers_and_send_posts_the_answer(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    assert ch.delivered == ["900", "901"] and len(ch.reactions()) == 2
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", "The test fails because mul adds."))
    assert result.success and [p for m, p, b in ch.forge.writes if p.endswith("/issues/1/comments")]


def test_dry_run_records_the_mention_and_sends_nothing(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path, dry_run=True)
    ch.poll()
    assert ch.delivered == [] and ch.forge.writes == [] and ch.answered() == []
    assert [e["key"] for e in ch.events("deliver_skipped")] == [f"{DEMO}#1/c900", f"{DEMO}#1/c901"]
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", "The test fails because mul adds."))
    assert not result.success and result.error.startswith("Preview is on") and ch.forge.writes == []
    ch.poll(after=30)  # both are still inside the overlap; they are not recorded a second time
    assert len(ch.events("deliver_skipped")) == 2 and ch.forge.writes == []


def test_a_mention_that_keeps_failing_is_given_up_after_three_tries(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path, comments=[comment(900, "@forgebruecke-bot first", "2026-11-08T13:00:10Z")])
    ch.refuse = {"900"}
    for _ in range(5):
        ch.poll()
    assert ch.delivered == ["900"] * 3
    assert ch.answered() == [f"{DEMO}#1/c900"]
    assert [e["tries"] for e in ch.events("deliver_failed")] == [1, 2, 3]


# --- send() posts only the answer to a mention the channel itself handed to Hermes -----------------------------

def test_send_refuses_a_thread_no_mention_started(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    result = asyncio.run(ch.adapter.send(f"{DEMO}#7", "posted by a cron delivery"))
    assert not result.success and "mention" in result.error and ch.comments() == []
    assert ch.events("send_refused")[-1]["chat"] == f"{DEMO}#7"


def test_send_refuses_a_thread_after_the_answer_window(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    ch.now += adapter_mod.ANSWER_WINDOW_S + 1
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", "an hour later"))
    assert not result.success and ch.comments() == []


def test_send_refuses_a_thread_whose_repo_rule_turned_mentions_off(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    ch.values["writes"] = [{"repo_glob": DEMO, "mentions": False}]
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", "answer"))
    assert not result.success and ch.comments() == []


def test_send_answers_the_thread_it_handed_over_within_the_window(adapter_mod, tmp_path):
    ch = Channel(adapter_mod, tmp_path)
    ch.poll()
    ch.now += adapter_mod.ANSWER_WINDOW_S - 60
    result = asyncio.run(ch.adapter.send(f"{DEMO}#1", "answer"))
    assert result.success and len(ch.comments()) == 1


def test_the_platform_refuses_every_send_message_and_cron_target():
    from fb_fakes import sub as load
    plugin = load("__init__")
    seen = {}

    class Ctx:
        def register_platform(self, **kw):
            seen.update(kw)

    plugin._register_channel(Ctx(), None)
    verdict = seen["validate_target_ref_fn"](f"{DEMO}#1")
    assert isinstance(verdict, str) and verdict and verdict is not True
