"""hermes forgebruecke: the tick's arguments, and the desk as text."""
import argparse

import pytest

from fb_fakes import DEMO, DictState, sub
from fb_fakes_tools_read import kit, plugin

loop = sub("flows.loop")


def cli(tmp_path, monkeypatch, **args):
    """Run one ``hermes forgebruecke`` command. Returns ``(exit code, ticks that ran)``."""
    forge, gs, sec = kit()
    ticks = []

    def run_tick(access, data_dir, source, only=None, runner=None):
        ticks.append(only)
        return {"ok": True, "prs": [], "writes": 0, "seconds": 0}
    monkeypatch.setattr(loop, "run_tick", run_tick)
    handler = plugin.make_cli_handler(gs, sec, state=DictState(tmp_path))
    return handler(argparse.Namespace(**{"json": False, **args})), ticks


@pytest.mark.parametrize("given,missing", [({"repo": DEMO, "number": None}, "--number"),
                                           ({"repo": None, "number": 3}, "--repo")])
def test_a_tick_for_half_a_pull_request_runs_nothing_and_says_what_is_missing(tmp_path, monkeypatch, capsys, given,
                                                                               missing):
    code, ticks = cli(tmp_path, monkeypatch, forgebruecke_command="tick", **given)
    assert code == 2 and ticks == []
    assert missing in capsys.readouterr().out


def test_a_tick_for_one_pull_request_or_for_all_still_runs(tmp_path, monkeypatch):
    code, ticks = cli(tmp_path, monkeypatch, forgebruecke_command="tick", repo=DEMO, number=3)
    assert code == 0 and ticks == [(DEMO, 3)]
    code, ticks = cli(tmp_path, monkeypatch, forgebruecke_command="tick", repo=None, number=None)
    assert code == 0 and ticks == [None]
