"""Backend routes for the Forgebrücke desk, mounted at `/api/plugins/forgebruecke/`.

    GET  /desk[?refresh=true]         rows (one per open PR with a flow), counts, repo cards; 15 s cache
    GET  /settings[?refresh=true]     the two key fields (set or not, whose account, scopes), the
                                      capability registry's features, the setups, the profiles; 60 s cache
    POST /merge                       {repo, number, head_sha}: the merge button, with the write token of
                                      your account, only when the registry says it can merge
    POST /setting                     {key, value}: the settings page's switches and "Use this login". Saves
                                      one of you, dry_run, review_every_pr, learn_mode, audit_enabled to the
                                      profile's plugin settings; never a token
    POST /reviewer-profile            create `forgebruecke-reviewer` (or the command)
    POST /reload-tokens               end this profile's plugin host child, so the next request reads the
                                      profile's .env again (only inside a plugin host child)
    POST /park                        {repo, number, note?, wake_commits, seen{head_sha, comment_at, draft}}:
                                      set a pull request aside under Waiting for me (his private desk state)
    POST /unpark                      {repo, number}: bring it back (Unpark, Undo)
    POST /comment                     {repo, number, body}: post his comment with his write token, as him,
                                      read back before it says posted

The handlers are `desk.py`'s `api_*` functions; this file only adapts them to FastAPI. Hermes loads it
as a standalone module, so the plugin folder is loaded here as a package by path. Settings come from
the profile config (`plugins.entries.forgebruecke.settings`), secrets from Hermes' secret scope. The
flow state is the agent half's data folder (`ctx.state.data_dir`, the portable namespace
`agent-plugin-forgebruecke-<digest>`), resolved per call so it follows the active profile.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Optional

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


def _data_dir(create: bool = False) -> Optional[Path]:
    """The agent half's data folder: the same name Hermes gives `ctx.state.data_dir`. ``create`` makes it for
    the park routes: a profile that never ran a flow has none yet."""
    try:
        from hermes_cli.plugins_manifest import _portable_data_namespace

        name = _portable_data_namespace(PLUGIN)
    except Exception:  # an older Hermes: the same rule, by hand
        name = f"agent-plugin-{PLUGIN}-{hashlib.sha256(PLUGIN.encode()).hexdigest()[:8]}"
    path = _home() / "plugin-data" / name
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


@router.post("/merge")
def merge(body: dict = Body(...)) -> JSONResponse:
    return _reply(_desk().api_merge(_get_setting, _get_secret, body, data_dir=_data_dir()))


@router.post("/setting")
def setting(body: dict = Body(...)) -> JSONResponse:
    try:
        from hermes_cli.plugins_state import save_plugin_setting
    except ImportError:
        return JSONResponse({"ok": False, "error": "not_supported",
                             "message": "This Hermes cannot save a setting from a page. Set it in Hermes' settings."},
                            status_code=501)

    def save(key: str, value: Any) -> None:
        save_plugin_setting(PLUGIN, (key,), value)

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
