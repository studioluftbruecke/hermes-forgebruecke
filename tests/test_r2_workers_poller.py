"""The mention poller reads the stretch before its cursor again on the poll after the cursor moved, at every
poll interval: Forgejo can list a comment after a poll already moved the cursor past its stamp."""
import pytest

from fb_fakes import BASE, DEMO, FakeForgejo, bot_account, make_token, sample_data, sub

poller = sub("channel.poller")
settings_mod = sub("settings")
client_mod = sub("client")
BOT = make_token("bot")
T0 = poller.parse_ts("2026-11-08T13:00:00Z")
DEFAULT_INTERVAL = settings_mod.load_settings(lambda k, d=None: {"url": BASE}.get(k, d)).poll_interval


def comment(cid, at):
    stamp = poller.iso(at)
    return {"id": cid, "user": {"login": "hermes"}, "body": "@forgebruecke-bot please", "created_at": stamp,
            "updated_at": stamp, "html_url": f"{BASE}/{DEMO}/issues/1#issuecomment-{cid}"}


class Forge:
    """Two issue threads the bot may answer in, and one ledger whose cursor starts at T0."""

    def __init__(self):
        data = sample_data()
        data[DEMO]["perms"] = {"hermes": "write"}
        data[DEMO]["issues"][2] = {**data[DEMO]["issues"][1], "number": 2, "title": "second thread"}
        data[DEMO]["comments"] = {1: [], 2: []}
        self.comments = data[DEMO]["comments"]
        self.forge = FakeForgejo({BOT: bot_account()}, data=data)
        self.settings = settings_mod.load_settings(lambda k, d=None: {"url": BASE, "writes": []}.get(k, d))
        self.client = client_mod.ForgejoClient(BASE, BOT, transport=self.forge)
        self.ledger = poller.Ledger({"since": poller.iso(T0)})

    def mention(self, cid, at, issue):
        self.comments[issue].append(comment(cid, at))

    def list_thread(self, issue, at):
        self.forge.notifications = [t for t in self.forge.notifications if t["id"] != issue] + [{
            "id": issue, "updated_at": poller.iso(at), "repository": {"full_name": DEMO},
            "subject": {"type": "Issue", "url": f"{BASE}/api/v1/repos/{DEMO}/issues/{issue}"}}]

    def poll(self, at):
        """One poll whose mentions were all handed over, as the adapter does it."""
        found = poller.find_mentions(self.client, bot="forgebruecke-bot", settings=self.settings, ledger=self.ledger,
                                     now=at)
        for m in found:
            self.ledger.mark(m.key, m.thread, at)
        return [m.comment_id for m in found]

    def thread_reads(self):
        return sum(1 for call in self.forge.calls if call[1].endswith("/comments"))


@pytest.mark.parametrize("interval", [poller.MIN_INTERVAL_S, 30, DEFAULT_INTERVAL, 600])
def test_a_mention_made_in_the_cursors_second_is_found_on_the_next_poll(interval):
    f = Forge()
    f.mention(900, T0 + 10, issue=1)
    f.list_thread(1, T0 + 10)
    assert f.poll(T0 + 10.5) == [900]
    f.mention(901, T0 + 10.9, issue=2)  # the same second as the cursor, after the poll read
    f.list_thread(2, T0 + 10.9)
    assert f.poll(T0 + 10.5 + interval) == [901]


@pytest.mark.parametrize("interval", [poller.MIN_INTERVAL_S, 30, DEFAULT_INTERVAL, 600])
def test_a_comment_listed_late_behind_a_cursor_another_thread_moved_is_found(interval):
    f = Forge()
    f.mention(901, T0 + 20, issue=2)  # made at +20, and Forgejo lists its thread only after the first poll
    f.mention(900, T0 + 21, issue=1)
    f.list_thread(1, T0 + 21)
    assert f.poll(T0 + 22) == [900]
    f.list_thread(2, T0 + 23)
    assert f.poll(T0 + 22 + interval) == [901]


def test_the_stretch_before_the_cursor_is_read_once_more_and_then_an_idle_forge_costs_no_thread_read():
    f = Forge()
    f.mention(900, T0 + 10, issue=1)
    f.list_thread(1, T0 + 10)
    assert f.poll(T0 + 11) == [900]
    assert f.poll(T0 + 11 + DEFAULT_INTERVAL) == []
    # The default interval (30 s since 0.0.21) is shorter than the overlap: polls inside OVERLAP_S after the move
    # read the stretch again; once it ran out, an idle forge costs no thread read.
    t = T0 + 11 + DEFAULT_INTERVAL
    while t - (T0 + 11) < poller.OVERLAP_S:
        t += DEFAULT_INTERVAL
        assert f.poll(t) == []
    after_the_reread = f.thread_reads()
    for i in range(1, 5):
        assert f.poll(t + DEFAULT_INTERVAL * i) == []
    assert f.thread_reads() == after_the_reread


def test_the_ledger_on_disk_remembers_that_the_next_poll_reads_the_overlap(tmp_path):
    f = Forge()
    f.mention(900, T0 + 10, issue=1)
    f.list_thread(1, T0 + 10)
    assert f.poll(T0 + 10.5) == [900]
    f.ledger.save(tmp_path / "ledger.json")
    f.ledger = poller.Ledger.load(tmp_path / "ledger.json")  # the gateway restarted between the two polls
    f.mention(901, T0 + 10.9, issue=2)
    f.list_thread(2, T0 + 10.9)
    assert f.poll(T0 + 10.5 + DEFAULT_INTERVAL) == [901]
