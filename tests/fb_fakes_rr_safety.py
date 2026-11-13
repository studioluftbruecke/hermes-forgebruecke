"""A container-style layout for the settings guard: the gateway's HOME is the root R, the profile is
R/profiles/coding (no word "hermes" in any of it), and Hermes gives agent shells HOME=<profile>/home.
``run`` executes a command in a real bash the way an agent shell does and returns which guarded files
changed."""
from __future__ import annotations

import hashlib
import http.server
import subprocess
import threading
from pathlib import Path

CONFIG = ("plugins:\n  enabled: [forgebruecke]\n  entries:\n    forgebruecke:\n      settings:\n"
          "        dry_run: true\n")


class Layout:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "srv" / "data"
        self.home = self.root / "profiles" / "coding"
        self.shell_home = self.home / "home"
        self.project = tmp_path / "srv" / "app"
        self.outside = tmp_path / "outside"
        for folder in (self.shell_home, self.home / "plugins", self.project, self.outside,
                       self.home / "plugin-data" / "forgebruecke" / "flows" / "overrides"):
            folder.mkdir(parents=True, exist_ok=True)
        self.reset()

    def reset(self) -> None:
        (self.home / "config.yaml").write_text(CONFIG, encoding="utf-8")
        (self.home / ".env").write_text("FORGEBRUECKE_BOT_ACCOUNT_TOKEN=x\n", encoding="utf-8")
        (self.home / "plugin-data" / "forgebruecke" / "flows" / "control.json").write_text("{}\n", encoding="utf-8")
        (self.project / "config.yaml").write_text("app: 1\n", encoding="utf-8")
        (self.project / ".env.example").write_text("A=1\n", encoding="utf-8")
        (self.outside / "evil.yml").write_text("flow: demo\n", encoding="utf-8")
        evil = self.outside / "evil"
        evil.mkdir(exist_ok=True)
        (evil / "config.yaml").write_text(CONFIG.replace("dry_run: true", "dry_run: false"), encoding="utf-8")
        if not (self.outside / "evil.tgz").exists():
            subprocess.run(["tar", "czf", str(self.outside / "evil.tgz"), "-C", str(evil), "config.yaml"], check=True)

    def digest(self) -> dict:
        out = {}
        for base in (self.home / "config.yaml", self.home / ".env", self.home / "plugin-data"):
            files = [base] if base.is_file() else sorted(p for p in base.rglob("*") if p.is_file())
            for f in files:
                out[str(f.relative_to(self.home))] = hashlib.sha256(f.read_bytes()).hexdigest()
        return out

    def run(self, command: str, cwd: "Path | None" = None) -> list:
        """Run ``command`` as an agent shell would and list the guarded files it changed."""
        self.reset()
        before = self.digest()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.shell_home), "HERMES_HOME": str(self.home)}
        subprocess.run(["bash", "-c", command], cwd=str(cwd or self.project), env=env, capture_output=True, timeout=60)
        after = self.digest()
        return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


class RecordingCtx:
    """A plugin context that records what ``register`` wires. ``cwd`` and ``surface`` answer
    ``ctx.session_cwd()`` and ``ctx.session_surface()``; ``platform_error`` makes ``register_platform`` raise as
    Hermes' plugin host does."""

    def __init__(self, state=None, *, settings=None, cwd: str = "", surface: str = "cli",
                 platform_error: "Exception | None" = None) -> None:
        self.tools, self.cli, self.hooks, self.platforms = {}, {}, {}, {}
        self.state = state
        self.settings = dict(settings or {})
        self.cwd, self.surface, self.platform_error = cwd, surface, platform_error

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, **kw):
        self.tools[kw["name"]] = kw

    def register_cli_command(self, **kw):
        self.cli[kw["name"]] = kw

    def register_hook(self, name, callback):
        self.hooks.setdefault(name, []).append(callback)

    def register_platform(self, **kw):
        if self.platform_error is not None:
            raise self.platform_error
        self.platforms[kw["name"]] = kw

    def session_cwd(self) -> str:
        return self.cwd

    def session_surface(self) -> str:
        return self.surface

    def current_cron_execution(self):
        return None


class ScriptedServer:
    """A real HTTP server on 127.0.0.1 that answers each request with the next ``(status, content_type, body)``
    and records ``(method, path, headers)``. A ``{token}`` in a body becomes the request's own token."""

    def __init__(self, *answers) -> None:
        self.answers, self.seen = list(answers), []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                owner.seen.append((self.command, self.path, dict(self.headers)))
                status, kind, body = owner.answers.pop(0) if owner.answers else (500, "text/plain", "no answer left")
                auth = self.headers.get("Authorization", "")
                data = body.replace("{token}", auth.split(" ", 1)[-1]).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _answer

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "ScriptedServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class DevLayout:
    """A profile that develops code, this plugin included, on a container host: the gateway's HOME is ``root``, the
    profile is root/profiles/coding with the agent shell's HOME at <profile>/home, a project and a checkout of this
    plugin live under <profile>/work, and the profile loads the plugin through plugins/forgebruecke, a link into the
    checkout. ``run`` executes a session's commands in one real bash (Hermes keeps the shell's folder between calls)
    and lists the guarded files they changed."""

    CONFIG = "plugins:\n  entries:\n    forgebruecke:\n      settings:\n        dry_run: true\n"

    def __init__(self, base: Path) -> None:
        self.root = base / "gw"
        self.home = self.root / "profiles" / "coding"
        self.shell_home = self.home / "home"
        self.project = self.home / "work" / "proj"
        self.checkout = self.home / "work" / "fb-repo"
        self.code = self.checkout / "plugins" / "forgebruecke"
        self.outside = base / "outside"
        self.cwds = {"none": None, "project": self.project, "profile": self.home, "gateway-home": self.root}
        self.reset()

    def reset(self) -> None:
        import shutil
        for folder in (self.home, self.outside):
            shutil.rmtree(folder, ignore_errors=True)
        files = {self.home / "config.yaml": self.CONFIG, self.home / ".env": "FORGEBRUECKE_BOT_ACCOUNT_TOKEN=x\n",
                 self.home / "plugin-data" / "forgebruecke" / "flows" / "control.json": "{}\n",
                 self.project / "config.yaml": "app: 1\n", self.project / ".env": "A=1\n",
                 self.project / "report.md": "r\n", self.code / "approval.py": "# code\n",
                 self.checkout / "compose.yml": "services: {}\n",
                 self.outside / "evil" / "config.yaml": self.CONFIG.replace("true", "false"),
                 self.outside / "go.sh": f"cd {self.home}\n"}
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        self.shell_home.mkdir(parents=True, exist_ok=True)
        (self.home / "plugins").mkdir(exist_ok=True)
        (self.home / "plugins" / "forgebruecke").symlink_to(self.code)
        subprocess.run(["tar", "czf", str(self.outside / "evil.tgz"), "-C", str(self.outside / "evil"), "config.yaml"],
                       check=True)

    def digest(self) -> dict:
        out = {}
        files = [self.home / "config.yaml", self.home / ".env", *sorted((self.home / "plugin-data").rglob("*")),
                 *sorted(self.code.rglob("*"))]
        for f in files:
            if f.is_file() and not f.is_symlink():
                out[str(f.relative_to(self.root))] = hashlib.sha256(f.read_bytes()).hexdigest()
        return out

    def run(self, commands: list, cwd: "Path | None") -> list:
        self.reset()
        before = self.digest()
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.shell_home), "HERMES_HOME": str(self.home)}
        subprocess.run(["bash", "-c", "\n".join(commands)], cwd=str(cwd or self.project), env=env,
                       capture_output=True, timeout=60)
        after = self.digest()
        return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
