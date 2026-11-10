"""ForgejoClient: one small, dependency-free client for the Forgejo REST API (v1).

Shapes this module owns:

- ``Response(status, data, headers)``: one answered request; ``data`` is parsed JSON or raw bytes.
- ``Page(items, total, has_more)``: a paginated listing. ``total`` comes from ``X-Total-Count`` when
  the forge sends it; ``has_more`` is True only when the forge holds items this page did not return.
- ``Capabilities``: the instance's version, product, whether this plugin supports it, and the set of
  API operations (``"GET /notifications"``) read from the instance's own swagger.
- ``ForgejoError(status, kind, message, scopes)``: every failure, with a sentence the user can act on.
  ``may_have_landed`` is True for a write whose answer did not arrive or was a 5xx. ``sent`` is False when
  the request never reached the forge.

Transport is injectable (``transport(method, url, headers, body, timeout) -> (status, headers,
bytes)``) so tests replay recorded shapes; the default uses ``urllib`` from the standard library and
never follows a redirect.
Retries: a GET up to 3 times, with backoff, on 5xx and 429 only. A write is sent once. A 4xx is never
retried. Nothing here logs or returns the token.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import math
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional

DEFAULT_TIMEOUT = 20.0
RETRIES = 3
BACKOFF = (0.5, 1.0, 2.0)
MAX_RETRY_AFTER = 30.0
CAPS_TTL = 600.0
PAGE_LIMIT = 50  # Forgejo's default max_response_items (GET /settings/api)
TESTED_FORGEJO = "15.0.11"
MIN_FORGEJO_MAJOR = 7  # first Forgejo major with every endpoint the v0.1 tools read
MIN_GITEA = (1, 21)
USER_AGENT = "forgebruecke"
MAY_HAVE_LANDED = "The write may have landed. Check before you repeat it."
_NEVER_CONNECTED = (ConnectionRefusedError, socket.gaierror)  # refused, or the name did not resolve

_SCOPE_RE = re.compile(r"required scope\(s\):\s*\[([^\]]*)\]")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")

Transport = Callable[[str, str, dict, Optional[bytes], float], "tuple[int, dict, bytes]"]


class ForgejoError(Exception):
    """A request the forge refused or could not answer. ``message`` is written for the user.
    ``may_have_landed`` marks a write that went out and whose answer did not arrive or was a failure."""

    def __init__(self, status: Optional[int], kind: str, message: str, *, scopes: tuple = (),
                 forge_message: str = "", may_have_landed: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.message = message
        self.scopes = tuple(scopes)
        self.forge_message = forge_message
        self.may_have_landed = may_have_landed

    @property
    def sent(self) -> bool:
        """False when the request never reached the forge: it was not sent, or no connection was made."""
        return self.status is not None or self.may_have_landed

    def to_dict(self) -> dict:
        out = {"status": self.status, "kind": self.kind, "message": self.message}
        if self.scopes:
            out["missing_scopes"] = list(self.scopes)
        return out


@dataclass(frozen=True)
class Response:
    status: int
    data: Any
    headers: dict


@dataclass(frozen=True)
class Page:
    items: list
    total: Optional[int]
    has_more: bool


@dataclass(frozen=True)
class Capabilities:
    raw_version: str
    product: str  # "forgejo", "gitea" or "unknown"
    version: tuple
    supported: bool
    reason: str
    operations: frozenset

    def has(self, method: str, path: str) -> bool:
        return f"{method.upper()} {path}" in self.operations

    def to_dict(self) -> dict:
        return {"version": self.raw_version, "product": self.product, "supported": self.supported,
                "reason": self.reason, "api_operations": len(self.operations),
                "tested_with": f"Forgejo {TESTED_FORGEJO}"}


def forge_message(data: Any) -> str:
    if isinstance(data, dict):
        msg = str(data.get("message") or "")
        errs = data.get("errors")
        if isinstance(errs, list) and errs and not msg:
            msg = "; ".join(str(e) for e in errs)
        return msg
    if isinstance(data, (bytes, str)):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        return text.strip()[:300]
    return ""


def missing_scopes(message: str) -> tuple:
    """Scopes named by Forgejo's 403 "token does not have at least one of required scope(s): [a b]".
    Measured on 15.0.11: the token needs every scope in the brackets, despite the wording."""
    m = _SCOPE_RE.search(message or "")
    return tuple(s for s in re.split(r"[\s,]+", m.group(1)) if s) if m else ()


def error_for(status: int, data: Any, *, write: bool = False) -> ForgejoError:
    """Map an HTTP failure to a sentence the user can act on. ``write`` is True for any method but GET."""
    msg = forge_message(data)
    if status == 401:
        return ForgejoError(401, "token_rejected",
                            "Token rejected: Forgejo does not accept this token. Check that it is "
                            "current and was created by the right account.", forge_message=msg)
    if status == 403:
        scopes = missing_scopes(msg)
        if scopes:
            return ForgejoError(403, "missing_scope",
                                f"This token cannot do this: it lacks the scope {', '.join(scopes)}. "
                                "Create a token that includes it.", scopes=scopes, forge_message=msg)
        return ForgejoError(403, "forbidden",
                            f"This token cannot do this: its account has no access to it ({msg or '403'}).",
                            forge_message=msg)
    if status == 404:
        return ForgejoError(404, "not_found", "Not found, or not visible to this token.", forge_message=msg)
    if status in (409, 422):
        return ForgejoError(status, "rejected", msg or f"Forgejo refused the request ({status}).",
                            forge_message=msg)
    if status == 429:
        unchanged = "; nothing was changed" if write else ""
        return ForgejoError(429, "rate_limited",
                            f"Forgejo is rate limiting this token{unchanged}. Try again later.", forge_message=msg)
    if status >= 500 and write:
        # A proxy can answer 502 or 504 after the forge stored the write.
        return ForgejoError(status, "unreachable", f"Forgejo failed ({status}). {MAY_HAVE_LANDED}",
                            forge_message=msg, may_have_landed=True)
    if status >= 500:
        return ForgejoError(status, "unreachable", f"Forgejo is unreachable or failing ({status}).",
                            forge_message=msg)
    return ForgejoError(status, "http_error", msg or f"Forgejo answered {status}.", forge_message=msg)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError, so the 3xx and its Location reach send_url


_NO_REDIRECT = urllib.request.build_opener(_NoRedirect)


def _unanswered(method: str, reason: Any) -> ForgejoError:
    """A request that got no whole answer. A write counts as possibly landed unless no connection was made:
    the forge may have stored it before the connection closed, the answer was cut short or the time ran out."""
    why = getattr(reason, "strerror", None) or str(reason) or type(reason).__name__
    if method == "GET":
        return ForgejoError(None, "unreachable", f"Forgejo is unreachable ({why}).")
    if isinstance(reason, _NEVER_CONNECTED):
        return ForgejoError(None, "unreachable", f"Forgejo is unreachable ({why}); nothing was changed.")
    if isinstance(reason, TimeoutError) or "timed out" in why:
        return ForgejoError(None, "timeout", f"Forgejo did not answer in time ({why}). {MAY_HAVE_LANDED}",
                            may_have_landed=True)
    return ForgejoError(None, "no_answer", f"Forgejo's answer did not arrive ({why}). {MAY_HAVE_LANDED}",
                        may_have_landed=True)


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes],
                     timeout: float, *, open_url: Optional[Callable] = None) -> "tuple[int, dict, bytes]":
    """One request with urllib. ``open_url`` defaults to ``urllib.request.urlopen``, which follows
    redirects. The client uses ``default_transport``, which does not."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        try:
            resp = (open_url or urllib.request.urlopen)(req, timeout=timeout)  # noqa: S310 (URL from settings)
            status = resp.status
        except urllib.error.HTTPError as e:
            resp, status = e, e.code  # an error status is an answer, and its body can be cut short like any other
        with resp:
            return status, {k.lower(): v for k, v in (resp.headers or {}).items()}, resp.read() or b""
    except (OSError, http.client.HTTPException) as e:
        raise _unanswered(method, getattr(e, "reason", e)) from None


def default_transport(method: str, url: str, headers: dict, body: Optional[bytes],
                      timeout: float) -> "tuple[int, dict, bytes]":
    """``urllib_transport`` that never follows a redirect. urllib would resend a redirected POST as a GET
    without its body and pass the token on to the new host."""
    return urllib_transport(method, url, headers, body, timeout, open_url=_NO_REDIRECT.open)


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    for suffix in ("/api/v1", "/api"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
    return url


def parse_version(raw: str) -> "tuple[str, tuple, bool, str]":
    """``(product, version, supported, reason)`` from ``GET /version``."""
    m = _VERSION_RE.match(raw or "")
    if not m:
        return "unknown", (), False, f"Forgejo reported an unreadable version {raw!r}."
    ver = tuple(int(p) for p in m.groups() if p is not None)
    if "+gitea" in raw or ver[0] >= 7:
        if ver[0] < MIN_FORGEJO_MAJOR:
            return "forgejo", ver, False, (f"Forgejo {raw} is older than {MIN_FORGEJO_MAJOR}.0, the "
                                           "oldest version this plugin supports. Upgrade Forgejo.")
        note = "" if raw.startswith(TESTED_FORGEJO) else f" Tested with {TESTED_FORGEJO}; features this Forgejo lacks are hidden."
        return "forgejo", ver, True, f"Forgejo {raw} is supported.{note}"
    if ver[0] == 1:
        ok = ver[:2] >= MIN_GITEA
        return "gitea", ver, ok, (f"Gitea {raw}: the Forgejo API it shares works, but only Forgejo is tested."
                                  if ok else f"Gitea {raw} is older than {MIN_GITEA[0]}.{MIN_GITEA[1]}. "
                                  "Upgrade it.")
    return "unknown", ver, False, f"Unrecognised Forgejo version {raw!r}."


def swagger_operations(spec: Any) -> frozenset:
    ops = set()
    paths = spec.get("paths") if isinstance(spec, dict) else None
    for path, methods in (paths or {}).items():
        if isinstance(methods, dict):
            for method in methods:
                if method.lower() in ("get", "post", "put", "patch", "delete"):
                    ops.add(f"{method.upper()} {path}")
    return frozenset(ops)


class ForgejoClient:
    """Talks to one Forgejo instance with one token. Sync, so tools and the CLI can call it directly.
    ``retries`` is the number of extra attempts for a GET; a caller with a deadline may set it lower."""

    def __init__(self, base_url: str, token: Optional[str], *, transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = normalize_base_url(base_url)
        if not self.base_url.startswith(("https://", "http://")):
            raise ForgejoError(None, "bad_url", "The Forgejo address must start with https:// "
                               f"(got {base_url!r}). Set it in Hermes' settings.")
        self._token = token or None
        self._transport = transport or default_transport
        self._sleep = sleep
        self._clock = clock
        self._timeout = timeout
        self._caps: Optional[Capabilities] = None
        self._caps_at = 0.0
        self.retries = RETRIES

    def __repr__(self) -> str:  # never the token
        return f"ForgejoClient({self.base_url!r}, token={'set' if self._token else 'unset'})"

    @property
    def account_key(self) -> str:
        """A cache key for "this forge, this token": a short one-way digest, never the token itself."""
        digest = hashlib.sha256((self._token or "").encode("utf-8")).hexdigest()[:16]
        return f"{self.base_url}|{digest}"

    # -- requests -------------------------------------------------------------------------------

    def _headers(self, has_body: bool) -> dict:
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self._token:
            h["Authorization"] = f"token {self._token}"
        if has_body:
            h["Content-Type"] = "application/json"
        return h

    def send_url(self, method: str, url: str, *, json_body: Any = None, raw: bool = False) -> Response:
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        headers = self._headers(body is not None)
        retries = self.retries if method == "GET" else 0  # a repeated write can land twice
        attempt = 0
        while True:
            status, resp_headers, payload = self._transport(method, url, headers, body, self._timeout)
            if (status == 429 or status >= 500) and attempt < retries:
                self._sleep(self._retry_delay(resp_headers, attempt))
                attempt += 1
                continue
            break
        if 300 <= status < 400:
            target = str(resp_headers.get("location") or "")[:90] or "an address it did not name"
            raise ForgejoError(status, "redirect",
                               f"This address answered with a redirect to {target} ({status}). The Forgejo address "
                               "setting is wrong. Set the address that answers without a redirect.")
        data: Any = payload
        if not raw:
            try:
                data = json.loads(payload.decode("utf-8")) if payload else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                if status < 400:  # a login page or another app answers 200 with HTML
                    raise ForgejoError(status, "not_forgejo", "This address did not answer like Forgejo. "
                                       "Check the Forgejo address.") from None
                data = payload.decode("utf-8", "replace")
        if status >= 400:
            raise error_for(status, data, write=method != "GET")
        return Response(status, data, resp_headers)

    @staticmethod
    def _retry_delay(headers: dict, attempt: int) -> float:
        try:
            seconds = float(headers.get("retry-after") or "")
        except ValueError:
            seconds = math.nan
        if math.isfinite(seconds):
            return min(max(seconds, 0.0), MAX_RETRY_AFTER)
        return BACKOFF[min(attempt, len(BACKOFF) - 1)]

    def url(self, path: str, params: Optional[dict] = None) -> str:
        # A proxy or the forge may resolve a dot segment, so the request would reach another object.
        if any(urllib.parse.unquote(segment) in (".", "..") for segment in path.split("/")):
            raise ForgejoError(None, "bad_path", "This request path has a . or .. segment and was not sent. "
                               "Name the repo, branch, tag or file without it.")
        q = {k: v for k, v in (params or {}).items() if v is not None}
        query = ("?" + urllib.parse.urlencode(q, doseq=True)) if q else ""
        return f"{self.base_url}/api/v1{path}{query}"

    def request(self, method: str, path: str, *, params: Optional[dict] = None, json_body: Any = None,
                raw: bool = False) -> Response:
        return self.send_url(method.upper(), self.url(path, params), json_body=json_body, raw=raw)

    def get(self, path: str, params: Optional[dict] = None, **kw) -> Response:
        return self.request("GET", path, params=params, **kw)

    def post(self, path: str, json_body: Any = None, **kw) -> Response:
        return self.request("POST", path, json_body=json_body, **kw)

    def patch(self, path: str, json_body: Any = None, **kw) -> Response:
        return self.request("PATCH", path, json_body=json_body, **kw)

    def delete(self, path: str, **kw) -> Response:
        return self.request("DELETE", path, **kw)

    def paginate(self, path: str, params: Optional[dict] = None, *, limit: int = PAGE_LIMIT,
                 max_items: int = 200) -> Page:
        """Follow ``page``/``limit`` until the forge runs out or ``max_items`` is reached. A forge may cap a
        page below ``limit``, so a short page ends the listing only when no total says otherwise."""
        items: list = []
        total: Optional[int] = None
        page = 1
        page_size = 0  # what page 1 held: the forge's real cap, or the whole list
        last: Optional[list] = None
        exhausted = False
        while len(items) < max_items:
            resp = self.get(path, {**(params or {}), "page": page, "limit": limit})
            batch = resp.data if isinstance(resp.data, list) else []
            tc = resp.headers.get("x-total-count")
            if tc is not None and str(tc).isdigit():
                total = int(tc)
            if not batch or batch == last:  # the same page twice: this endpoint ignores ``page``
                exhausted = True
                break
            items.extend(batch)
            page_size = page_size or len(batch)
            if total is not None:
                exhausted = len(items) >= total
            else:  # only page 2 can tell a capped first page from a complete list
                exhausted = page > 1 and len(batch) < page_size
            if exhausted:
                break
            last, page = batch, page + 1
        fetched = len(items)
        items = items[:max_items]
        has_more = fetched > len(items) or (not exhausted and (total is None or total > len(items)))
        return Page(items, total, has_more)

    # -- capabilities ---------------------------------------------------------------------------

    def capabilities(self, *, refresh: bool = False) -> Capabilities:
        """Version and API operations of the instance, cached for 10 minutes."""
        now = self._clock()
        if self._caps is not None and not refresh and now - self._caps_at < CAPS_TTL:
            return self._caps
        raw = str((self.get("/version").data or {}).get("version") or "")
        product, ver, supported, reason = parse_version(raw)
        try:
            spec = self.send_url("GET", f"{self.base_url}/swagger.v1.json").data
            ops = swagger_operations(spec)
        except ForgejoError as e:
            ops = frozenset()
            reason += f" The API description (/swagger.v1.json) could not be read: {e.message}"
        self._caps = Capabilities(raw, product, ver, supported, reason, ops)
        self._caps_at = now
        return self._caps
