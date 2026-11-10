"""Fakes for the desk's own routes: a forge that also knows users and organisations."""
import json
import urllib.parse


class UsersForge:
    """Wraps a transport and answers ``GET /users/{login}`` and ``GET /orgs/{login}`` itself.

    ``users`` and ``orgs`` hold logins. Forgejo answers ``/users/{login}`` for an organisation too, so an
    organisation is found on both routes. ``down`` makes both routes answer 500.
    """

    def __init__(self, inner, *, users=(), orgs=(), down=False):
        self.inner, self.users, self.orgs, self.down = inner, set(users), set(orgs), down
        self.asked: list = []

    @staticmethod
    def _json(status, body):
        return status, {"content-type": "application/json"}, json.dumps(body).encode()

    def __call__(self, method, url, headers, body, timeout):
        path = urllib.parse.urlsplit(url).path
        seg = path[len("/api/v1"):].strip("/").split("/") if path.startswith("/api/v1") else []
        if method == "GET" and len(seg) == 2 and seg[0] in ("users", "orgs"):
            self.asked.append("/".join(seg))
            if self.down:
                return self._json(500, {"message": "internal error"})
            known = self.orgs if seg[0] == "orgs" else self.users | self.orgs
            match = next((name for name in known if name.lower() == seg[1].lower()), None)
            if match is None:
                return self._json(404, {"message": "The target couldn't be found."})
            return self._json(200, {"login": match, "username": match})
        return self.inner(method, url, headers, body, timeout)


class OneRouteFails:
    """Wraps a transport and answers 500 on one path, for every token or for one."""

    def __init__(self, inner, path, token=None):
        self.inner, self.path, self.token = inner, path, token

    def __call__(self, method, url, headers, body, timeout):
        mine = self.token is None or headers.get("Authorization") == "token " + self.token
        if urllib.parse.urlsplit(url).path == self.path and mine:
            return 500, {"content-type": "application/json"}, json.dumps({"message": "internal error"}).encode()
        return self.inner(method, url, headers, body, timeout)
