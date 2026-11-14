"""Backend routes for the Forgebrücke desk, mounted at `/api/plugins/forgebruecke/`.

    GET  /desk[?refresh=true]         rows (one per open PR with a flow), counts, repo cards; 15 s cache
    GET  /settings[?refresh=true]     the two key fields (set or not, whose account, scopes), the
                                      capability registry's features, the setups, the profiles; 60 s cache
    GET  /repo-pulls?repo=owner/name  one repo's open pull requests (checks, Merge by the desk's rule) and its
                                      default branch's latest commit, for the Repos tab on expand; 15 s cache
    POST /merge                       {repo, number, head_sha}: the merge button, with the write token of
                                      your account, only when the registry says it can merge
    POST /setting                     {key, value}: the settings page's switches and "Use this login". Saves
                                      one of you, dry_run, review_every_pr, learn_mode, audit_enabled to the
                                      profile's plugin settings; never a token
    POST /reviewer-profile            create `forgebruecke-reviewer` (or the command)
    POST /reload-tokens               read Forgebrücke's two tokens again through Hermes' secret loader and
                                      use them in this process; ends nothing
    POST /park                        {repo, number, note?, wake_commits, seen{head_sha, comment_at, draft}}:
                                      set a pull request aside under Waiting for me (the user's private desk state)
    POST /unpark                      {repo, number}: bring it back (Unpark, Undo)
    POST /comment                     {repo, number, body}: post the user's comment with the user's write token,
                                      read back before it says posted

The handlers are `desk.py`'s `api_*` functions; this file only adapts them to FastAPI. Hermes loads it
as a standalone module, so the plugin folder is loaded here as a package by path. Settings come from
the profile config (`plugins.entries.forgebruecke.settings`), secrets from Hermes' secret scope. The
flow state is the agent half's data folder (`ctx.state.data_dir`, the portable namespace
`agent-plugin-forgebruecke-<digest>`), resolved per call so it follows the active profile. The setting route
writes through Hermes' own `hermes config set` (a subprocess with this profile's HERMES_HOME).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

PLUGIN = "forgebruecke"
_PACKAGE = "hermes_forgebruecke_dashboard"

router = APIRouter()


def _plugin():
    module = sys.modules.get(_PACKAGE)
    if module is not None:
        return module
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(_PACKAGE, root / "__init__.py",
                                                  submodule_search_locations=[str(root)])
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load the {PLUGIN} package")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def _desk():
    _plugin()
    return importlib.import_module(f"{_PACKAGE}.desk")


def _get_setting(key: str, default: Any = None) -> Any:
    from hermes_cli.config import load_config_readonly

    entries = ((load_config_readonly() or {}).get("plugins") or {}).get("entries") or {}
    settings = (entries.get(PLUGIN) or {}).get("settings") or {}
    return settings.get(key, default)


def _get_secret(name: str) -> Optional[str]:
    from agent.secret_scope import get_secret

    return get_secret(name)


def _home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def data_namespace(key: str = PLUGIN) -> str:
    """The folder name Hermes gives a plugin's `ctx.state.data_dir` (`plugin-data/agent-plugin-<slug>-<digest>`),
    restated because Hermes has no public call for it; a test holds it equal to Hermes' own."""
    slug = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "_-") else "-" for ch in key.lower()).strip("-_")
    return f"agent-plugin-{slug or 'plugin'}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}"


def _data_dir(create: bool = False) -> Optional[Path]:
    """The agent half's data folder. ``create`` makes it for the park routes: a profile that never ran a flow
    has none yet."""
    path = _home() / "plugin-data" / data_namespace()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path if path.is_dir() else None


def _reply(result: "tuple[int, dict]") -> JSONResponse:
    status, body = result
    return JSONResponse(body, status_code=status)


# Sync handlers: FastAPI runs them on a worker thread, so a slow forge never blocks the loop.

@router.get("/desk")
def desk(refresh: bool = Query(False)) -> JSONResponse:
    return _reply(_desk().api_desk(_get_setting, _get_secret, data_dir=_data_dir(), refresh=refresh))


@router.get("/settings")
def settings(refresh: bool = Query(False)) -> JSONResponse:
    return _reply(_desk().api_settings(_get_setting, _get_secret, refresh=refresh, home=_home(), data_dir=_data_dir()))


@router.get("/repo-pulls")
def repo_pulls(repo: str = Query(""), refresh: bool = Query(False)) -> JSONResponse:
    return _reply(_desk().api_repo_pulls(_get_setting, _get_secret, repo, data_dir=_data_dir(), refresh=refresh))


@router.post("/merge")
def merge(body: dict = Body(...)) -> JSONResponse:
    return _reply(_desk().api_merge(_get_setting, _get_secret, body, data_dir=_data_dir()))


_CLI_WORDS = {"true", "false", "yes", "no", "on", "off", "null", "none", "~", ""}


def _as_cli_value(value: Any) -> str:
    """``value`` as `hermes config set` writes it back unchanged. That command reads true/false and numbers as
    such, so a name it would turn into one cannot be saved from the page."""
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    try:
        float(text)
        number = True
    except ValueError:
        number = False
    if number or text.strip().lower() in _CLI_WORDS:
        raise PermissionError(f"Hermes would read {text!r} as a value, not a name. Set it in Hermes' settings.")
    return text


MANAGED_REFUSAL = "Plugin settings cannot be changed in a managed install"
ADMINISTRATOR_REFUSAL = "This plugin setting is managed by your Hermes administrator"
LOCKED_REFUSAL = "This plugin setting is locked by Remote Config"
NOT_SAVED = "Hermes did not save the setting; the profile's config holds another value"


def _known_refusal(output: str) -> Optional[str]:
    """Recognize Hermes' refusals without presenting its startup output as the reason."""
    text = output.lower()
    if "managed by your administrator" in text or "administrator-managed" in text:
        return ADMINISTRATOR_REFUSAL
    if "locked by the " in text and " level" in text:
        return LOCKED_REFUSAL
    if ("this hermes installation is managed" in text or "this install is managed" in text
            or "plugin settings cannot be changed in a managed install" in text):
        return MANAGED_REFUSAL
    return None


def _json_value(output: str) -> Any:
    """One scalar from `config get --json`, tolerating a warning line before it."""
    candidates = [output, *(line for line in reversed(output.splitlines()) if line.strip())]
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (TypeError, ValueError):
            continue
    raise ValueError("config get returned no JSON value")


def save_setting(key: str, value: Any, *, home: Path, run: Callable = subprocess.run) -> None:
    """Write one setting with Hermes' CLI, then prove the requested value landed."""
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    path = f"plugins.entries.{PLUGIN}.settings.{key}"
    done = run([sys.executable, "-m", "hermes_cli.main", "config", "set", path, _as_cli_value(value)],
               env=env, capture_output=True, text=True, timeout=60)
    refusal = _known_refusal(str(getattr(done, "stdout", "") or "") + "\n" + str(getattr(done, "stderr", "") or ""))
    if refusal is not None:
        raise PermissionError(refusal)
    if getattr(done, "returncode", 1) != 0:
        raise PermissionError(NOT_SAVED)
    read = run([sys.executable, "-m", "hermes_cli.main", "config", "get", path, "--json"],
               env=env, capture_output=True, text=True, timeout=60)
    if getattr(read, "returncode", 1) != 0:
        raise PermissionError(NOT_SAVED)
    try:
        actual = _json_value(getattr(read, "stdout", ""))
    except (TypeError, ValueError):
        raise PermissionError(NOT_SAVED) from None
    if type(actual) is not type(value) or actual != value:
        raise PermissionError(NOT_SAVED)


@router.post("/setting")
def setting(body: dict = Body(...)) -> JSONResponse:
    home = _home()

    def save(key: str, value: Any) -> None:
        save_setting(key, value, home=home)

    return _reply(_desk().api_set_setting(body, save=save, get_setting=_get_setting, get_secret=_get_secret,
                                          data_dir=_data_dir()))


@router.post("/reviewer-profile")
def reviewer_profile(body: dict = Body(default={})) -> JSONResponse:
    home, d = _home(), _desk()
    return _reply(d.api_create_reviewer(body, source=d._mod("profile").current_name(home), home=home))


@router.post("/reload-tokens")
def reload_tokens(body: dict = Body(default={})) -> JSONResponse:
    return _reply(_desk().api_reload_tokens(data_dir=_data_dir()))


@router.post("/park")
def park(body: dict = Body(...)) -> JSONResponse:
    return _reply(_desk().api_park(body, data_dir=_data_dir(create=True)))


@router.post("/comment")
def comment(body: dict = Body(...)) -> JSONResponse:
    return _reply(_desk().api_comment(_get_setting, _get_secret, body, data_dir=_data_dir()))


@router.post("/unpark")
def unpark(body: dict = Body(...)) -> JSONResponse:
    return _reply(_desk().api_unpark(body, data_dir=_data_dir(create=True)))
