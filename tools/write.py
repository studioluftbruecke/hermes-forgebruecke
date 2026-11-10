"""Write tools: forgejo_comment, forgejo_create_pr, forgejo_status.

Every write is planned first (``plan_*``) and executed only after Hermes' approval gate passed the
exact plan (``approval.py``). A plan is pure data: repo, the HTTP requests in order, the text the
human approves, and a signature the handler compares before sending. Each planner first checks the
Forgejo right (``access.check_write``): writes need the agent account's write access on the repo.
Writes always use the agent token.

What does not exist here on purpose: no review ``event`` other than ``COMMENT`` (never approve,
never request changes on someone's behalf), and no merge request of any kind. The only merge the
plugin will ever send is the flow engine's ``promote`` gate into a non-protected branch.
"""
from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

FOOTER = "\n\n---\n<sub>Posted by Hermes through Forgebrücke.</sub>"
MAX_TEXT = 60_000
STATES = ("pending", "success", "failure", "error")
_CONTEXT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")  # SHA-1 or SHA-256, never a short form
# The head goes into a URL path, so no dot-dot, no empty segment, and none of the endings git refuses.
_BRANCH_RE = re.compile(r"^(?!/)(?!.*(?:\.\.|//))[A-Za-z0-9._/-]{1,200}(?<![/.])$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
WRITE_TOOLS = ("forgejo_comment", "forgejo_create_pr", "forgejo_status")
PREVIEW_CHARS = 1500


def _mod(name: str):
    pkg = (__package__ or "").rpartition(".")[0] or __package__
    return importlib.import_module(f".{name}", pkg)


def _refuse(code: str, message: str, setting: str = ""):
    return _mod("access").Refusal(code, message, setting)


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: dict

    def signature(self) -> tuple:
        return (self.method, self.path, json.dumps(self.body, sort_keys=True))


@dataclass(frozen=True)
class WritePlan:
    tool: str
    repo: str
    requests: tuple  # of Request; the first one creates, the rest follow up on it
    summary: str     # one line: what this writes where
    preview: str = ""
    extra: dict = field(default_factory=dict, compare=False)
    quotes_forge: bool = field(default=False, compare=False)  # the summary quotes text a forge user wrote

    def signature(self) -> tuple:
        return ((self.tool, self.repo) + tuple(r.signature() for r in self.requests)
                + (json.dumps(self.extra, sort_keys=True),))

    def approval_text(self, bot: str) -> str:
        # The CLI card shows about ten rows of this, so the text the human approves comes first.
        if not self.requests:  # a local change (a flow override, a resume): nothing goes to the forge
            text = f"This changes Forgebrücke's flows in this profile: {self.summary.rstrip('.')}."
        else:
            text = f"This writes to {self.repo} as {bot}: {self.summary.rstrip('.')}."
        if self.preview:
            p = self.preview if len(self.preview) <= PREVIEW_CHARS else self.preview[:PREVIEW_CHARS] + "…"
            text += f"\n\n{p}"
        return text


def _text(args: dict, key: str, what: str, *, required: bool = True) -> str:
    value = str(args.get(key) or "").strip()
    if required and not value:
        raise _refuse("bad_args", f"Give the {what} ({key}).")
    if len(value) > MAX_TEXT:
        raise _refuse("bad_args", f"The {what} is longer than {MAX_TEXT} characters.")
    return value


def _number(args: dict) -> int:
    try:
        n = int(args.get("number"))
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    raise _refuse("bad_args", "Give the issue or pull request number (number).")


def plan_comment(access, args: dict) -> WritePlan:
    acc = _mod("access")
    repo = acc.parse_repo(args.get("repo"))
    access.check_write(repo, "comment")
    n = _number(args)
    body = _text(args, "body", "comment") + FOOTER
    path_arg = str(args.get("path") or "").strip()
    reader = access.reader()
    issue = reader.get(f"/repos/{repo}/issues/{n}").data or {}
    what = "PR" if issue.get("pull_request") else "issue"
    title = issue.get("title") or ""
    if path_arg:
        if what != "PR":
            raise _refuse("bad_args", f"#{n} is an issue; a line comment (path, line) needs a pull request.")
        try:
            line = int(args.get("line"))
            if line < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise _refuse("bad_args", "A line comment needs line, the line number in the new file.") from None
        pos_key = "old_position" if str(args.get("side") or "new") == "old" else "new_position"
        req = Request("POST", f"/repos/{repo}/pulls/{n}/reviews",
                      {"event": "COMMENT", "body": "", "comments": [{"path": path_arg, pos_key: line, "body": body}]})
        summary = f"a line comment on {path_arg}:{line} of PR #{n} \"{title}\""
    else:
        req = Request("POST", f"/repos/{repo}/issues/{n}/comments", {"body": body})
        summary = f"a comment on {what} #{n} \"{title}\""
    return WritePlan("forgejo_comment", repo, (req,), summary, _text(args, "body", "comment"), quotes_forge=True)


def plan_create_pr(access, args: dict) -> WritePlan:
    acc = _mod("access")
    repo = acc.parse_repo(args.get("repo"))
    rule = access.check_write(repo, "write")
    head = _text(args, "head", "source branch")
    if not _BRANCH_RE.match(head):
        raise _refuse("bad_args", f"{head!r} is not a branch name.")
    reader = access.reader()
    info = reader.get(f"/repos/{repo}").data or {}
    default = info.get("default_branch") or "main"
    base = str(args.get("base") or default).strip()
    allowed = list(rule.targets) or [default]
    if base not in allowed:
        fix = (f"The rule {rule.repo_glob!r} allows {', '.join(allowed)} (targets). Add {base} to its targets."
               if rule.targets else
               f"Without a rule that names targets, only the default branch {default} is allowed. "
               f"Add a rule {{repo_glob: {repo}, targets: [{base}]}}.")
        raise _refuse("not_allowed", f"Pull requests into {base} of {repo} are not allowed. {fix}", "writes")
    if head == base:
        raise _refuse("bad_args", "The source branch and the target branch are the same.")
    try:
        reader.get(f"/repos/{repo}/branches/{head}")
    except _mod("client").ForgejoError as e:
        if e.status != 404:
            raise
        raise _refuse("no_branch", f"Branch {head} is not on {repo}. Push it first.") from None
    title = _text(args, "title", "title")[:255]
    body = _text(args, "body", "description", required=False) + FOOTER
    payload: dict = {"head": head, "base": base, "title": title, "body": body}
    labels = [str(x).strip() for x in (args.get("labels") or []) if str(x).strip()]
    if labels:
        known = {l.get("name"): l.get("id") for l in reader.paginate(f"/repos/{repo}/labels", max_items=500).items}
        missing = [l for l in labels if l not in known]
        if missing:
            raise _refuse("bad_args", f"{repo} has no label {', '.join(missing)}; existing: "
                                      f"{', '.join(sorted(k for k in known if k)) or 'none'}.")
        payload["labels"] = sorted(known[l] for l in labels)
    requests = [Request("POST", f"/repos/{repo}/pulls", payload)]
    you = access.settings.you
    if "reviewers" in args and args.get("reviewers") is not None:
        raw = args.get("reviewers")
        raw = [raw] if isinstance(raw, str) else raw
        reviewers = [str(r).strip().lstrip("@") for r in (raw or []) if str(r).strip()]
        asked_by = "as asked"
    else:  # Hermes asks you to review every pull request it opens, unless the call names reviewers
        reviewers = [you] if you else []
        asked_by = "you, by default"
    bad = [r for r in reviewers if not _USER_RE.match(r)]
    if bad:
        raise _refuse("bad_args", f"Not user names: {', '.join(bad)}.")
    summary = f"a pull request {head} → {base}, \"{title}\""
    if reviewers:
        summary += f", asking {', '.join(reviewers)} to review ({asked_by})"
    elif you:
        summary += ", asking no one to review"
    preview = (f"Labels: {', '.join(labels)}\n" if labels else "") + (_text(args, "body", "description", required=False)
                                                                      or "(no description)")
    return WritePlan("forgejo_create_pr", repo, tuple(requests), summary, preview,
                     extra={"reviewers": reviewers})


def plan_status(access, args: dict) -> WritePlan:
    acc = _mod("access")
    repo = acc.parse_repo(args.get("repo"))
    access.check_write(repo, "write")
    sha = str(args.get("sha") or "").strip().lower()
    if not _SHA_RE.match(sha):
        raise _refuse("bad_args", "Give the full commit sha (sha) the status belongs to.")
    state = str(args.get("state") or "").strip()
    if state not in STATES:
        raise _refuse("bad_args", f"state must be one of {', '.join(STATES)}.")
    context = str(args.get("context") or "forgebruecke/agent").strip()
    if not _CONTEXT_RE.match(context):
        raise _refuse("bad_args", f"{context!r} is not a status context like forgebruecke/review.")
    description = " ".join(str(args.get("description") or "").split())[:140]
    payload = {"context": context, "state": state, "description": description}
    target_url = str(args.get("target_url") or "").strip()
    if target_url:
        if not target_url.startswith(("https://", "http://")):
            raise _refuse("bad_args", "target_url must be an http(s) address.")
        payload["target_url"] = target_url
    req = Request("POST", f"/repos/{repo}/statuses/{sha}", payload)
    return WritePlan("forgejo_status", repo, (req,), f"commit status {context} = {state} on {sha}",
                     description or "(no description)")


PLANNERS = {"forgejo_comment": plan_comment, "forgejo_create_pr": plan_create_pr, "forgejo_status": plan_status}


def plan(access, tool: str, args: dict) -> WritePlan:
    if tool not in PLANNERS:
        raise _refuse("bad_args", f"{tool} is not a write tool.")
    return PLANNERS[tool](access, args if isinstance(args, dict) else {})


def noted(result: dict) -> dict:
    """``result`` with the read tools' note: it quotes text a forge user wrote (a title, a finding, a file)."""
    return {**result, "note": _mod("tools.read").DATA_NOTE}


def _result(p: WritePlan, fields: dict) -> dict:
    result = {**fields, "summary": p.summary}
    return noted(result) if p.quotes_forge else result


# What each tool creates, and the forgejo_list kind that shows whether it is there.
_MADE = {"forgejo_comment": ("comment", "comments"), "forgejo_create_pr": ("pull request", "prs"),
         "forgejo_status": ("status", "statuses")}


def execute(access, p: WritePlan) -> dict:
    """Send the approved plan with the agent token. Returns what the forge answered, never the token."""
    writer = access.writer()
    ForgejoError = _mod("client").ForgejoError
    first = p.requests[0]
    made, list_kind = _MADE[p.tool]
    if first.path.endswith("/reviews"):  # a line comment is listed with its review
        list_kind = "reviews"
    try:
        resp = writer.request(first.method, first.path, json_body=first.body)
    except ForgejoError as e:
        message = f"{e.message} forgejo_list kind={list_kind} shows it." if e.may_have_landed else e.message
        return _result(p, {"ok": False, "sent": e.sent, "error": e.kind, "status": e.status, "message": message})
    if not isinstance(resp.data, dict):  # a create answers with the new object; a list is the collection, read
        return _result(p, {"ok": False, "sent": True, "error": "bad_answer", "status": resp.status,
                           "message": f"Forgejo answered {resp.status} without the new {made}. "
                                      f"Check {p.repo} before you repeat it."})
    data = resp.data
    out = _result(p, {"ok": True, "sent": True, "status": resp.status, "tool": p.tool, "repo": p.repo,
                      "id": data.get("id"), "url": data.get("html_url") or data.get("target_url") or None})
    if p.tool == "forgejo_create_pr":
        out.update(number=data.get("number"), head_sha=(data.get("head") or {}).get("sha"))
        reviewers = p.extra.get("reviewers") or []
        if reviewers and data.get("number"):
            try:
                writer.post(f"/repos/{p.repo}/pulls/{data['number']}/requested_reviewers", {"reviewers": reviewers})
                out["review_requested"] = reviewers
            except ForgejoError as e:
                out["review_request_failed"] = e.message
    if p.tool == "forgejo_status":
        out.update(context=data.get("context"), state=data.get("status") or data.get("state"))
    return out


def preview(p: WritePlan) -> dict:
    """What a call answers while dry_run is on. Not ok, so the model cannot read it as a sent write."""
    return _result(p, {"ok": False, "error": "dry_run", "sent": False,
                       "message": "Preview only. Nothing was sent. The user switches on Let Hermes write on the "
                                  "Forgebrücke page."})
