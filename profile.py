"""Which Hermes profile this turn or adapter belongs to, and whether Forgebrücke is on there.

Hermes keeps some registries process-wide: an `@forgejo:` provider or a `pre_tool_call` hook registered
by one profile's load is seen by every profile the gateway serves. So the plugin asks, at call time,
whether it is enabled in the profile whose turn is running, and stays silent where it is not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

PLUGIN = "forgebruecke"
PLATFORM = "forgebruecke"


def current_home() -> Optional[Path]:
    """The Hermes home of the running turn (the multiplexer binds it per profile), else None."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return None


def read_config(home: Path) -> dict:
    """The profile's config.yaml, read fresh each time (a few KB). Raises when it cannot be read.

    No cache on purpose: in the gateway a cache keyed on the file's mtime returned the state from
    before a `hermes config set` (measured), so a switch the user flipped was not seen."""
    path = Path(home) / "config.yaml"
    try:
        from hermes_cli.config import read_config_doc  # Hermes' own reader of a profile's config file
    except ImportError:  # outside Hermes (the plugin's own tests without it)
        import yaml

        def read_config_doc(p):
            return yaml.safe_load(Path(p).read_text(encoding="utf-8"))
    data = read_config_doc(path) or {}
    return data if isinstance(data, dict) else {}


def enabled_in(config: dict, name: str = PLUGIN) -> bool:
    plugins = config.get("plugins") if isinstance(config, dict) else None
    if not isinstance(plugins, dict):
        return False
    enabled = plugins.get("enabled") or []
    disabled = plugins.get("disabled") or []
    return name in enabled and name not in disabled


def enabled_here() -> bool:
    """True when the running profile enables the plugin, or when that cannot be told (CLI tests)."""
    home = current_home()
    if home is None:
        return True
    try:
        return enabled_in(read_config(home))
    except FileNotFoundError:
        return True
    except Exception:
        return True


def settings_of(config: dict) -> dict:
    entry = (((config.get("plugins") or {}).get("entries") or {}).get(PLUGIN) or {})
    return entry.get("settings") or {}


def setting_getter(config: dict):
    values = settings_of(config)

    def get(key: str, default: Any = None) -> Any:
        return values.get(key, default)

    return get


# Tools and Hermes toolsets that run commands, write files, start other agents or jobs, write the memory every later
# session reads, or read the owner's past sessions. A mention turn starts on a Forgejo commenter's text, so none of
# them may be in its toolset. Every `hermes-<platform>` bundle carries the shell; `hermes-webhook` is the one
# read-only bundle. MCP toolsets are blocked too: their names do not tell us whether the server offers shell or files.
HOST_TOOLS = frozenset({"terminal", "process_manage", "execute_code", "write_file", "patch", "delegate_task",
                        "cronjob_manage", "skill_manage", "browser_exec", "browser_cdp", "computer_use", "memory",
                        "session_search"})
HOST_TOOLSETS = frozenset({"terminal", "file", "code_execution", "debugging", "coding", "delegation", "cronjob",
                           "skills", "browser", "computer_use", "kanban", "memory", "session_search"})


def reaches_host(name: str) -> bool:
    name = str(name).strip().lower()
    return (name in HOST_TOOLS or name in HOST_TOOLSETS or name == "mcp" or name.startswith("mcp-")
            or (name.startswith("hermes-") and name != "hermes-webhook"))


def channel_toolsets(config: dict) -> Optional[list]:
    value = (config.get("platform_toolsets") or {}).get(PLATFORM)
    return list(value) if isinstance(value, (list, tuple)) else None


def channel_blockers(config: dict, profile_name: str = "") -> list:
    """Why the mention channel must not run in this profile, as sentences. Empty means it may.
    With ``profile_name`` the commands carry ``-p <name>``: a bare ``hermes config set`` in the user's own
    shell writes the default profile, not the one that runs the plugin."""
    out = []
    hermes = f"hermes -p {profile_name}" if profile_name else "hermes"
    if not enabled_in(config):
        out.append("the forgebruecke plugin is not enabled in this profile")
    platform = (config.get("platforms") or {}).get(PLATFORM)
    if not (isinstance(platform, dict) and platform.get("enabled", True) is not False):
        out.append(f"the profile has no platforms.{PLATFORM} block ({hermes} config set platforms.{PLATFORM} "
                   "'{enabled: true}')")
    toolsets = channel_toolsets(config)
    if toolsets is None:
        out.append(f"platform_toolsets.{PLATFORM} is not set, so a mention turn would get Hermes' default "
                   f"tools, shell included; set it to the tools a Forgejo commenter may trigger, for example "
                   f"{hermes} config set platform_toolsets.{PLATFORM} '[forgebruecke]'")
    else:
        risky = [str(t) for t in toolsets if reaches_host(t)]
        if risky:
            out.append(f"platform_toolsets.{PLATFORM} holds {', '.join(risky)}, which can run commands, write files, "
                       f"start agents or reach Hermes' memory on this host for any Forgejo commenter; remove them, "
                       f"for example "
                       f"{hermes} config set platform_toolsets.{PLATFORM} '[forgebruecke]'")
    return out


def list_profiles(home: Optional[Path] = None) -> Optional[list]:
    """Names of the Hermes profiles on this host (``default`` plus ``profiles/*``), or None when unknown.
    The review gate takes any of them."""
    home = home or current_home()
    if home is None:
        return None
    home = Path(home)
    root = home.parent.parent if home.parent.name == "profiles" else home
    names = {"default"} if (root / "config.yaml").exists() else set()
    profiles = root / "profiles"
    if profiles.is_dir():
        names |= {p.name for p in profiles.iterdir() if p.is_dir() and (p / "config.yaml").exists()}
    return sorted(names)


def profiles_root(home: Optional[Path] = None) -> Optional[Path]:
    """``<root>/profiles`` for the Hermes root this profile lives in."""
    home = home or current_home()
    if home is None:
        return None
    home = Path(home)
    root = home.parent.parent if home.parent.name == "profiles" else home
    return root / "profiles"


def current_name(home: Optional[Path] = None) -> str:
    """This profile's name: ``default`` for the root home, else the folder name under ``profiles/``."""
    home = home or current_home()
    if home is None:
        return ""
    home = Path(home)
    return home.name if home.parent.name == "profiles" else "default"
