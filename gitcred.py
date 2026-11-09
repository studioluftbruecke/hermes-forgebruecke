"""A git credential helper backed by the agent account token, so git in any shell on the host can
push and pull as Hermes' Forgejo account without a second copy of the token.

Git runs ``hermes -p <profile> forgebruecke git-credential get`` and writes the request on stdin
(``protocol=https``, ``host=forge.example``, one ``key=value`` per line, a blank line at the end).
``answer`` replies with ``username=`` and ``password=`` only when the request names the configured
forge (same scheme, same host and port) and the agent token is set. Every other request gets no
reply, so git asks its next helper. The personal write token is never handed out.

Shapes:

- ``Origin(scheme, host)``: the forge as git names it, ``host`` lower-cased and carrying a port only
  when it is not the scheme's default (git sends ``host=forge.example:3000`` the same way).
- a request: the ``dict`` of the ``key=value`` lines git sent.
"""
from __future__ import annotations

import shlex
import urllib.parse
from dataclasses import dataclass
from typing import Mapping, Optional

ACTIONS = ("get", "store", "erase")
CACHE_HELPER = "cache --timeout=3600"
DEFAULT_USERNAME = "forgebruecke"
_DEFAULT_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str


def forge_origin(url: str) -> Optional[Origin]:
    """The configured forge address as git names it, or None when it is not an http(s) address."""
    parts = urllib.parse.urlsplit((url or "").strip())
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    if port is not None and port != _DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{port}"
    return Origin(parts.scheme, host)


def parse_request(text: str) -> dict:
    request: dict = {}
    for line in (text or "").splitlines():
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            request[key] = value
    return request


def answer(request: Mapping[str, str], forge_url: str, token: Optional[str]) -> str:
    """The reply to one ``get``: the agent token for the configured forge, else nothing."""
    origin, token = forge_origin(forge_url), (token or "").strip()
    if origin is None or not token:
        return ""
    if request.get("protocol", "").lower() != origin.scheme:
        return ""
    if request.get("host", "").lower() != origin.host:
        return ""
    username = request.get("username") or DEFAULT_USERNAME
    return f"username={username}\npassword={token}\n"


def setup_text(forge_url: str, profile: str, hermes: str) -> str:
    """How to point git at this helper, with this profile and this host's ``hermes`` filled in."""
    origin = forge_origin(forge_url)
    if origin is None:
        return ("Set the Forgejo address in Forgebrücke's settings first; git needs it to know which "
                "requests this helper answers.\n")
    key = shlex.quote(f"credential.{origin.scheme}://{origin.host}.helper")
    # Git sends `store` to every helper after each success; starting Hermes for a no-op costs seconds.
    ours = shlex.quote(f'!f() {{ if [ "$1" = get ]; then exec {hermes} -p {profile} forgebruecke '
                       'git-credential get; fi; }; f')
    lines = [f"git config --global --replace-all {key} ''",
             f"git config --global --add {key} {shlex.quote(CACHE_HELPER)}",
             f"git config --global --add {key} {ours}"]
    return "\n".join([
        f"Git can push and pull at {origin.scheme}://{origin.host} as Hermes' Forgejo account, with the "
        f"agent account token of the profile {profile}. Run these three lines in the shell whose git "
        "should use it (they write that shell's ~/.gitconfig):",
        "",
        *(f"  {line}" for line in lines),
        "",
        "The empty helper line makes Forgebrücke the only helper for this forge; other hosts keep "
        "theirs. The cache keeps the answer in memory for an hour, so git does not start Hermes on "
        "every push. For one clone that several profiles share, run the same lines with "
        "`git -C <clone> config --local` in place of `git config --global`.",
        "",
    ])
