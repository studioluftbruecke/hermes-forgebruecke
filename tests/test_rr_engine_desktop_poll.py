"""When the sidebar count's poll is due: only while Desktop is visible, and no faster than the backend's own
poll interval. The running plugin is driven in tests/test_v0022_sidebar.py."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

JS = pathlib.Path(__file__).resolve().parents[1] / "desktop" / "plugin.js"
SOURCE = JS.read_text(encoding="utf-8")
NODE = shutil.which("node")


def due(last_ms, now_ms, hidden, poll_s):
    fn = re.search(r"export function navPollDue\(.*?\n\}", SOURCE, re.S).group(0)
    script = (SOURCE[SOURCE.index("const NAV_POLL_MS"):].split("\n", 1)[0] + "\n" + fn.replace("export ", "")
              + f"\nprocess.stdout.write(JSON.stringify(navPollDue({last_ms}, {now_ms}, {json.dumps(hidden)}, {json.dumps(poll_s)})))")
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_hidden_desktop_polls_nothing_and_a_visible_one_at_the_slower_interval():
    assert due(0, 60_000, True, 30) is False
    assert due(0, 60_000, False, 30) is True
    assert due(0, 120_000, False, 300) is False
    assert due(0, 300_000, False, 300) is True
    assert due(0, 1_000, False, None) is False

