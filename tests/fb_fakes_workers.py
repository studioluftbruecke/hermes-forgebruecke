"""Fakes for the review runner, the review requests and the mention channel: a transport that injects
faults, and stand-ins for the three gateway modules the channel adapter imports (the test venv has no Hermes).
"""
from __future__ import annotations

import json
import types
import urllib.parse


class Faulty:
    """Wraps a transport. Each rule is ``(predicate(method, path), answer)``; the answer is a ``(status, body)``
    pair or a callable ``(method, url, headers, body, timeout, inner) -> response``. ``times`` limits a rule."""

    def __init__(self, inner):
        self.inner = inner
        self.rules: list = []

    def fail(self, predicate, status=500, body=None, *, times=None):
        self.rules.append([predicate, (status, body or {"message": "boom"}), times])

    def answer(self, predicate, respond, *, times=None):
        self.rules.append([predicate, respond, times])

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/api/v1", 1)[-1].split("?", 1)[0]
        for rule in self.rules:
            predicate, answer, times = rule
            if times == 0 or not predicate(method, path):
                continue
            if times is not None:
                rule[2] = times - 1
            if callable(answer):
                return answer(method, url, headers, body, timeout, self.inner)
            return answer[0], {"content-type": "application/json"}, json.dumps(answer[1]).encode()
        return self.inner(method, url, headers, body, timeout)


def paged_notifications(forge):
    """A transport over ``FakeForgejo`` that pages ``GET /notifications`` like Forgejo does: newest first,
    ``page`` and ``limit`` honoured. The fake itself answers that route in one piece."""
    def transport(method, url, headers, body, timeout):
        parts = urllib.parse.urlsplit(url)
        if method != "GET" or parts.path != "/api/v1/notifications":
            return forge(method, url, headers, body, timeout)
        query = dict(urllib.parse.parse_qsl(parts.query))
        forge.calls.append((method, parts.path, query, body))
        threads = sorted((t for t in forge.notifications if t.get("updated_at", "") >= query.get("since", "")),
                         key=lambda t: t.get("updated_at", ""), reverse=True)
        page, limit = int(query.get("page", 1)), int(query.get("limit", 50))
        return 200, {"content-type": "application/json"}, json.dumps(threads[(page - 1) * limit: page * limit]).encode()
    return transport


class _BaseAdapter:
    def __init__(self, config=None, platform=None):
        self.config, self.platform, self._running = config, platform, False

    def build_source(self, **kw):
        return kw

    def _mark_connected(self):
        pass

    def _mark_disconnected(self):
        pass

    def _wire_plugin_handlers(self, native=None):  # Hermes' base adapter has it (gateway/platforms/base.py)
        pass


class _SendResult:
    def __init__(self, success, message_id=None, error=None, raw_response=None):
        self.success, self.message_id, self.error, self.raw_response = success, message_id, error, raw_response


class _MessageEvent:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def gateway_stubs() -> dict:
    """``{module name: module}`` for what ``channel/adapter.py`` imports from Hermes' gateway."""
    attrs = {"gateway": {}, "gateway.platforms": {},
             "gateway.config": {"Platform": lambda value: value, "PlatformConfig": object},
             "gateway.platforms.base": {"BasePlatformAdapter": _BaseAdapter, "SendResult": _SendResult},
             "gateway.platforms.event": {"MessageEvent": _MessageEvent,
                                         "MessageType": types.SimpleNamespace(TEXT="text")}}
    modules = {}
    for name, values in attrs.items():
        modules[name] = types.ModuleType(name)
        modules[name].__dict__.update(values)
    return modules
