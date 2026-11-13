"""The page opens only http(s) addresses outside Desktop, whatever string a row carries."""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

SOURCE = (pathlib.Path(__file__).resolve().parents[1] / "desktop" / "plugin.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("href, opens", [
    ("https://forge.example.test/acme/app/pulls/3", True), ("http://forge.lan/x", True),
    ("javascript:alert(1)", False), ("file:///etc/passwd", False), ("hermes://settings", False),
    ("", False), (None, False), ("https://x y", False)])
def test_only_web_addresses_are_opened(href, opens):
    found = re.search(r"export function isWebAddress\(href\) \{.*?\n\}", SOURCE, re.S)
    assert found, "no isWebAddress in desktop/plugin.js"
    script = found.group(0).replace("export ", "") + f"\nprocess.stdout.write(JSON.stringify(isWebAddress({json.dumps(href)})))"
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout) is opens
