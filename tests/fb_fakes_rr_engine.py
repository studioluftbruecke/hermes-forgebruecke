"""Fakes for the review worker's transport: a stand-in for the ``hermes`` binary that answers like a review
worker does, and a wait helper. The stand-in is a real executable, so the runner starts it as it starts Hermes."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def fake_hermes(folder: Path, *, answer: "str | None" = None, verdict: str = "pass", summary: str = "mul is fine",
                findings: "list | tuple" = (), stderr: str = "session_id: 20261113_000000_abcdef") -> Path:
    """An executable that records its argv to ``argv.json`` beside it, prints ``stderr`` to stderr and answers on
    stdout. Given ``answer``, it prints that text as is; without, a verdict JSON for the head commit the prompt
    names, as a well-behaved worker would."""
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / "hermes"
    script.write_text(f"""#!{sys.executable}
import json, re, sys
from pathlib import Path
here = Path(__file__).resolve().parent
(here / "argv.json").write_text(json.dumps(sys.argv[1:]))
prompt = Path(sys.argv[sys.argv.index("--query-file") + 1]).read_text(encoding="utf-8")
sys.stderr.write({stderr!r} + "\\n")
answer = {answer!r}
if answer is None:
    head = re.search(r"head commit ([0-9a-f]{{40}})", prompt).group(1)
    answer = json.dumps({{"verdict": {verdict!r}, "head_sha": head, "summary": {summary!r},
                         "findings": {list(findings)!r}}})
sys.stdout.write(answer + "\\n")
""", encoding="utf-8")
    script.chmod(0o755)
    return script


def argv_of(script: Path) -> list:
    return json.loads((script.parent / "argv.json").read_text())


def wait_for(test, seconds: float = 10.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if test():
            return True
        time.sleep(0.05)
    return bool(test())




def list_posted_comments(kit, pr: int = 3, others: list = ()) -> None:
    """Let the fake forge list a pull request's comments as Forgejo does: ``others`` first, then every comment
    posted through the API, with the body of its last edit."""
    def answer(inner, method, url, headers, body, timeout):
        posted = [c for c in kit.data.get("posted_comments", {}).values() if f"/issues/{pr}#" in c["html_url"]]
        for m, p, b in kit.forge.writes:
            if m == "PATCH" and "/issues/comments/" in p:
                for c in posted:
                    if p.endswith(f"/{c['id']}"):
                        c["body"] = b["body"]
        items = list(others) + posted
        return 200, {"content-type": "application/json", "x-total-count": str(len(items))}, json.dumps(items).encode()

    kit.transport.on(lambda m, p: m == "GET" and p.endswith(f"/issues/{pr}/comments"), answer)


def stored_but_failed(kit, match) -> None:
    """The forge stores the request and answers 502, as a proxy in front of a slow Forgejo does."""
    def answer(inner, method, url, headers, body, timeout):
        inner(method, url, headers, body, timeout)
        return 502, {"content-type": "text/html"}, b"<html>bad gateway</html>"

    kit.transport.on(match, answer)


def profiles(root: Path, **enabled: "list[str]") -> Path:
    """A Hermes root with one profile per keyword (``default`` is the root itself), each enabling the plugins
    named, and the home of the first profile, as the running one."""
    homes = []
    for name, plugins in enabled.items():
        home = root if name == "default" else root / "profiles" / name
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(f"plugins:\n  enabled: [{', '.join(plugins)}]\n", encoding="utf-8")
        homes.append(home)
    if not (root / "config.yaml").exists():
        (root / "config.yaml").write_text("{}\n", encoding="utf-8")
    return homes[0]


def review_home(root: Path) -> Path:
    """The running profile's home on a host whose review profile (the engine tests' ``forgebruecke-dev``) enables
    Forgebrücke, as a real runner needs before it starts a worker there."""
    return profiles(root, coding=["forgebruecke"], **{"forgebruecke-dev": ["forgebruecke"]})
