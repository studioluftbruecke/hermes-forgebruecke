"""``forgejo_repos``: the repo overview, best-practice packs, protection presets and the audit.

    overview  read. One repo: owner, visibility, your access, protection summary, open PRs, last activity.
              Without ``repo``: a short row per repo, with ``shown``, ``total`` and ``has_more``
    pack      write (approval): labels through the API, and files (issue and PR templates, CODEOWNERS,
              a README skeleton when there is none) as one commit on a new branch plus a pull request,
              never a push to the default branch
    preset    write (approval): one named set of the 25 branch-protection fields, shown as a diff first;
              only where the agent's account is admin of the repo
    audit     read, only with ``audit_enabled``: at most five findings per repo, each with a link and one
              suggested fix
    import    write (approval): ``POST /repos/migrate`` from a public source into the agent's own account,
              or the owner the call names, with the items asked for; then a report of what came over
    mirror    write (approval): a push mirror from a repo to another repo on this forge, synced once
    create_from_template  write (approval): ``POST /repos/{template}/generate`` with
              ``protected_branch: true``, then a read-back of the new repo's protection

No action checks the new repo's owner; Forgejo refuses a namespace the account may not create in.

Data shapes: ``Preset{name, label, fields{...}}`` (fields are Forgejo's CreateBranchProtectionOption);
``Finding{rule, severity, repo, title, detail, link, fix}``; ``PackItem{kind: file|label, path|name, text|color}``.
Tokens: reads in the caller's lane; pack and preset writes with the agent token only.
"""
from __future__ import annotations

import base64
import importlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Optional

TOOL = "forgejo_repos"
ACTIONS = ("overview", "audit", "import", "mirror", "create_from_template", "pack", "preset")
GATED = ("pack", "preset", "import", "mirror", "create_from_template")
SERVICES = ("git", "github", "gitea", "gitlab", "gogs", "onedev", "gitbucket", "codebase", "forgejo")
IMPORT_ITEMS = ("issues", "pull_requests", "labels", "milestones", "releases", "wiki", "lfs")
_NAME = __import__("re").compile(r"^[A-Za-z0-9_.-]{1,100}$")
MAX_FINDINGS = 5
MAX_REPOS = 30  # overview and audit without a repo: the rules' repos, else this many the agent account sees
MIGRATE_TIMEOUT_S = 240.0
POLL_TRIES = 12
POLL_WAIT_S = 10.0
_sleep = time.sleep
STALE_DAYS = 365
PACK_BRANCH = "forgebruecke/pack"

SCHEMA = {
    "name": TOOL,
    "description": (
        "Repos: overview, audit, import, mirror, create_from_template, pack, preset. overview: access, "
        "protection and open PRs of one repo, or of up to 30. audit: read-only, once the user switched it on. "
        "The other five write and ask the user first. import: a public repo with its issues and PRs. mirror: "
        "push to another repo here. pack: starter files and labels as a PR. preset: branch protection, needs "
        "admin."),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "repo": {"type": "string", "description": "owner/name (create_from_template: the template repo); "
                                                      "overview and audit default to the rules' repos"},
            "preset": {"type": "string", "enum": ["solo-with-agents", "team-of-two"],
                       "description": "pack and preset: which working style (default solo-with-agents)"},
            "branch": {"type": "string", "description": "preset: branch to protect (default: the default branch)"},
            "checks": {"type": "array", "items": {"type": "string"},
                       "description": "preset: required check contexts"},
            "second": {"type": "string", "description": "pack, team-of-two: second user name for CODEOWNERS"},
            "source": {"type": "string", "description": "import: https clone URL of a public repo"},
            "service": {"type": "string", "enum": list(SERVICES),
                        "description": "import: source host type (default from the URL)"},
            "name": {"type": "string", "description": "import and create_from_template: the new repo's name"},
            "owner": {"type": "string",
                      "description": "import, create_from_template: new owner (default: Hermes' account)"},
            "items": {"type": "array", "items": {"type": "string", "enum": list(IMPORT_ITEMS)},
                      "description": "import: what to bring besides code (default: all but wiki, lfs)"},
            "private": {"type": "boolean", "description": "import and create_from_template: private (default true)"},
            "target": {"type": "string", "description": "mirror: owner/name on this Forgejo that receives the pushes"},
        },
        "required": ["action"],
    },
}


def _mod(name: str):
    return importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.{name}")


def _refuse(code: str, message: str, setting: str = ""):
    return _mod("access").Refusal(code, message, setting)


# --- protection presets --------------------------------------------------------------------------------

PRESETS = {
    "solo-with-agents": {
        "label": "Solo with agents",
        "about": "Only you push to and merge into the branch; agents work on branches and wait for the checks.",
        "fields": {"enable_push": True, "enable_push_whitelist": True, "push_whitelist_usernames": ["{owner}"],
                   "enable_merge_whitelist": True, "merge_whitelist_usernames": ["{owner}"],
                   "enable_status_check": True, "status_check_contexts": ["{checks}"], "required_approvals": 0,
                   "block_on_rejected_reviews": True, "dismiss_stale_approvals": True,
                   "block_on_outdated_branch": False, "apply_to_admins": False},
    },
    "team-of-two": {
        "label": "Team of two",
        "about": "Nobody pushes to the branch; every change is a pull request with the checks and one approval.",
        "fields": {"enable_push": False, "enable_push_whitelist": False, "push_whitelist_usernames": [],
                   "enable_merge_whitelist": False, "merge_whitelist_usernames": [],
                   "enable_status_check": True, "status_check_contexts": ["{checks}"], "required_approvals": 1,
                   "block_on_rejected_reviews": True, "dismiss_stale_approvals": True,
                   "block_on_outdated_branch": True, "apply_to_admins": False},
    },
}
_WORDS = {
    "enable_push": lambda v: "direct pushes allowed" if v else "no direct pushes",
    "push_whitelist_usernames": lambda v: "who may push: " + (", ".join(v) or "nobody"),
    "enable_merge_whitelist": lambda v: "only listed people merge" if v else "anyone with write merges",
    "merge_whitelist_usernames": lambda v: "who may merge: " + (", ".join(v) or "anyone with write"),
    "enable_status_check": lambda v: "required checks on" if v else "required checks off",
    "status_check_contexts": lambda v: "required checks " + (", ".join(v) or "none"),
    "required_approvals": lambda v: f"{v} approval" + ("" if v == 1 else "s"),
    "block_on_rejected_reviews": lambda v: "a rejecting review blocks" if v else "rejecting reviews do not block",
    "dismiss_stale_approvals": lambda v: "new pushes dismiss approvals" if v else "approvals survive new pushes",
    "block_on_outdated_branch": lambda v: "the branch must be up to date" if v else "outdated branches may merge",
    "apply_to_admins": lambda v: "admins too" if v else "admins may bypass",
    "enable_push_whitelist": lambda v: "push list on" if v else "push list off",
}


def preset_fields(name: str, owner: str, checks: list) -> dict:
    if name not in PRESETS:
        raise _refuse("bad_args", f"Unknown preset {name!r}; use solo-with-agents or team-of-two.")
    out = {}
    for key, value in PRESETS[name]["fields"].items():
        if value == ["{owner}"]:
            value = [owner]
        elif value == ["{checks}"]:
            value = list(checks)
        out[key] = value
    return out


def protection_diff(branch: str, current: Optional[dict], wanted: dict) -> "tuple[dict, list]":
    """``(changed_fields, lines)``: only the fields that differ, and one line per change a person reads."""
    created = current is None
    current = current or {}
    changed = dict(wanted) if created else {
        k: v for k, v in wanted.items() if current.get(k) != v
        and not (isinstance(v, list) and sorted(current.get(k) or []) == sorted(v))}
    lines = []
    for k, v in changed.items():
        word = _WORDS.get(k, lambda x: f"{k} = {x}")
        before = word(current[k]) if k in current and current[k] is not None else "not set"
        lines.append(f"`{branch}`: {word(v)} (now: {before})")
    if created:
        lines.insert(0, f"`{branch}` has no protection rule yet; this creates one.")
    return changed, lines


# --- packs ---------------------------------------------------------------------------------------------

LABELS = (("bug", "#C8372D", "Something does not work"), ("enhancement", "#2E9E5B", "A new feature or a change"),
          ("agent", "#4100F5", "Work done or proposed by an agent"), ("needs-human", "#C98A00", "Waits for a person"))


def pack_items(repo: str, preset: str, owner: str, second: str = "") -> list:
    """Everything a pack would add, before checking what the repo already has."""
    name = repo.split("/", 1)[1]
    owners = f"@{owner}" + (f" @{second}" if preset == "team-of-two" and second else "")
    files = {
        ".forgejo/issue_template/bug.md": (
            "---\nname: Bug\nabout: Something does not work\nlabels: [\"bug\"]\n---\n\n"
            "## What happened\n\n## What you expected\n\n## How to see it again\n1.\n"),
        ".forgejo/issue_template/task.md": (
            "---\nname: Task\nabout: A change you or an agent should make\nlabels: [\"enhancement\"]\n---\n\n"
            "## Goal\n\n## Done when\n- [ ]\n"),
        ".forgejo/pull_request_template.md": (
            "## What changes\n\n## Why\n\n## How it was checked\n- [ ] Tests or a manual check, named here\n"
            + ("- [ ] One approval from the other person\n" if preset == "team-of-two" else
               "- [ ] Agent changes: the checks are green before you merge\n")),
        ".forgejo/CODEOWNERS": f"# Who reviews what. Forgejo asks these people for a review.\n* {owners}\n",
        "README.md": (f"# {name}\n\nWhat this is, in one sentence.\n\n## Start\n\n```bash\n# how to run it\n```\n\n"
                      "## How changes land\n\nEvery change is a pull request. "
                      + ("Agents open them; you merge after the checks pass.\n" if preset == "solo-with-agents"
                         else "One approval and the checks, then merge.\n")),
    }
    return ([{"kind": "file", "path": p, "text": t} for p, t in files.items()]
            + [{"kind": "label", "name": n, "color": c, "description": d} for n, c, d in LABELS])


def _exists(reader, repo: str, path: str, ref: str) -> bool:
    ForgejoError = _mod("client").ForgejoError
    try:
        reader.get(f"/repos/{repo}/contents/{path}", {"ref": ref})
        return True
    except ForgejoError as e:
        if e.status == 404:
            return False
        raise


def _readme_exists(reader, repo: str, ref: str) -> bool:
    return any(_exists(reader, repo, p, ref) for p in ("README.md", "README", "readme.md", "README.rst"))


# --- the gated spec ----------------------------------------------------------------------------------

def make_spec():
    write = _mod("tools.write")
    Request, WritePlan = write.Request, write.WritePlan

    class ReposSpec:
        @staticmethod
        def gated(args: Any) -> bool:
            return isinstance(args, dict) and str(args.get("action") or "") in GATED

        @staticmethod
        def plan(access, args: dict):
            action = str(args.get("action"))
            if action == "import":
                return _plan_import(access, args)
            if action == "create_from_template":
                return _plan_template(access, args)
            repo = _mod("access").parse_repo(args.get("repo"))
            if action == "mirror":
                return _plan_mirror(access, repo, args)
            access.check_write(repo, "write")
            reader = access.reader()
            info = reader.get(f"/repos/{repo}").data or {}
            owner = (info.get("owner") or {}).get("login") or repo.split("/")[0]
            base = info.get("default_branch") or "main"
            preset = str(args.get("preset") or "solo-with-agents")
            if preset not in PRESETS:
                raise _refuse("bad_args", f"Unknown preset {preset!r}; use solo-with-agents or team-of-two.")
            if action == "pack":
                return _plan_pack(access, reader, repo, owner, base, preset, str(args.get("second") or ""))
            return _plan_preset(access, repo, info, owner, base, preset, args)

        @staticmethod
        def execute(access, p) -> dict:
            writer = access.writer()
            if p.extra.get("action") in ("import", "create_from_template"):
                # a migration copies a whole project and can take minutes
                writer = _mod("client").ForgejoClient(access.settings.url, access.creds.token,
                                                      transport=access.transport, timeout=MIGRATE_TIMEOUT_S)
            ForgejoError = _mod("client").ForgejoError
            done = []
            try:
                for r in p.requests:
                    body = r.body
                    if p.extra.get("action") == "mirror" and "remote_password" in body:
                        # the token goes to Forgejo only here, never into the plan, the card or a log
                        body = {**body, "remote_password": access.creds.token}
                    resp = writer.request(r.method, r.path, json_body=body)
                    done.append((r.method, r.path, resp.data))
            except ForgejoError as e:
                if e.may_have_landed and p.extra.get("action") == "import":
                    return _poll_import(access, p, e)
                return {"ok": False, "sent": bool(done) or e.may_have_landed, "error": e.kind, "status": e.status,
                        "message": e.message, "done": [f"{m} {path}" for m, path, _ in done]}
            last = done[-1][2] if done else {}
            if p.extra.get("action") in ("import", "create_from_template", "mirror"):
                return _after_create(access, p, last)
            if p.extra.get("action") == "pack":
                return {"ok": True, "sent": True, "number": (last or {}).get("number"),
                        "url": (last or {}).get("html_url"), "labels": p.extra.get("labels"),
                        "files": p.extra.get("files"),
                        "message": "The pack waits in a pull request; merging it is the user's click."}
            return {"ok": True, "sent": True, "branch": p.extra.get("branch"), "changes": p.extra.get("lines"),
                    "message": "Protection saved. forgejo_repos action=overview shows it."}

    def _plan_pack(access, reader, repo, owner, base, preset, second):
        items = pack_items(repo, preset, owner, second)
        have_labels = {str(lbl.get("name", "")).lower()
                       for lbl in reader.paginate(f"/repos/{repo}/labels", max_items=500).items}
        files = [i for i in items if i["kind"] == "file"
                 and not (_readme_exists(reader, repo, base) if i["path"] == "README.md"
                          else _exists(reader, repo, i["path"], base))]
        labels = [i for i in items if i["kind"] == "label" and i["name"].lower() not in have_labels]
        if not files and not labels:
            raise _refuse("nothing_to_do", f"{repo} already has every file and label of the {preset} pack.")
        reqs = [Request("POST", f"/repos/{repo}/labels", {"name": i["name"], "color": i["color"],
                                                          "description": i["description"]}) for i in labels]
        if files:
            reqs.append(Request("POST", f"/repos/{repo}/contents", {
                "branch": base, "new_branch": PACK_BRANCH, "message": f"Forgebrücke: {preset} pack",
                "files": [{"operation": "create", "path": i["path"],
                           "content": base64.b64encode(i["text"].encode("utf-8")).decode("ascii")} for i in files]}))
            listing = "\n".join(f"- `{i['path']}`" for i in files)
            reqs.append(Request("POST", f"/repos/{repo}/pulls", {
                "head": PACK_BRANCH, "base": base, "title": f"Forgebrücke pack: {PRESETS[preset]['label']}",
                "body": (f"Adds the {PRESETS[preset]['label']} pack. {PRESETS[preset]['about']}\n\n{listing}\n\n"
                         "Merging it is your call; nothing here touches the branch protection." + write.FOOTER)}))
        what = []
        if labels:
            what.append("labels " + ", ".join(i["name"] for i in labels) + " (created at once, nothing on a branch)")
        if files:
            what.append(f"{len(files)} files on a new branch {PACK_BRANCH} and a pull request {PACK_BRANCH} → {base} "
                        f"(never a push to {base})")
        preview = "\n\n".join(f"--- {i['path']}\n{i['text']}" for i in files)
        return WritePlan(TOOL, repo, tuple(reqs), "the " + preset + " pack: " + "; ".join(what), preview,
                         extra={"action": "pack", "labels": [i["name"] for i in labels],
                                "files": [i["path"] for i in files]})

    def _plan_preset(access, repo, info, owner, base, preset, args):
        if not (info.get("permissions") or {}).get("admin"):
            right = _mod("access").right_from(info.get("permissions"))
            link = access.collaboration_url(repo)
            raise _mod("access").Refusal(
                "needs_admin", f"Presets need admin on {repo}. Hermes' account has {right} there. "
                               f"Apply it in Forgejo (Settings → Branches) or grant admin: {link}", link=link)
        branch = str(args.get("branch") or base)
        checks = [str(c) for c in (args.get("checks") or [])]
        if not checks:
            rule = access.settings.explicit_rule(repo)
            checks = ["forgebruecke/*"] if rule is not None and rule.flows else []
        wanted = preset_fields(preset, owner, checks)
        if not checks:
            wanted["enable_status_check"] = False
        rules = access.writer().get(f"/repos/{repo}/branch_protections").data or []
        current = next((r for r in rules if (r.get("rule_name") or r.get("branch_name")) == branch), None)
        changed, lines = protection_diff(branch, current, wanted)
        if not changed:
            raise _refuse("nothing_to_do", f"`{branch}` in {repo} already matches the {preset} preset.")
        if current is None:
            req = Request("POST", f"/repos/{repo}/branch_protections", {"rule_name": branch, **wanted})
        else:
            req = Request("PATCH", f"/repos/{repo}/branch_protections/{branch}", changed)
        return WritePlan(TOOL, repo, (req,), f"the {PRESETS[preset]['label']} protection preset on `{branch}`: "
                         + "; ".join(lines), "\n".join(lines),
                         extra={"action": "preset", "branch": branch, "lines": lines})

    def _new_repo_owner(access, args) -> "tuple[str, bool]":
        """``(owner, own)``: the owner the call names, else the agent's own account, and whether it is that
        account. Nothing here checks the owner; Forgejo refuses a namespace the account may not create in."""
        me = _mod("access").account_login(access.writer())
        owner = str(args.get("owner") or me).strip()
        return owner, owner.lower() == me.lower()

    def _scopes_missing_to_create(access) -> tuple:
        """The scopes Forgejo names when the token may not create a repo in its own account, else ``()``.
        An empty POST /user/repos creates nothing: Forgejo checks the scope, then refuses the missing name."""
        try:
            access.writer().post("/user/repos", {})
        except _mod("client").ForgejoError as e:
            return e.scopes if e.kind == "missing_scope" else ()
        return ()

    def _plan_import(access, args):
        source = str(args.get("source") or "").strip()
        if not source.startswith("https://") or any(c in source for c in "@ \n"):
            raise _refuse("bad_args", "source is the https clone URL of a public repo, without credentials.")
        host = source.split("/")[2].lower()
        service = str(args.get("service") or ("github" if host == "github.com" else "gitlab" if "gitlab" in host
                                               else "git"))
        if service not in SERVICES:
            raise _refuse("bad_args", f"service is one of {', '.join(SERVICES)}.")
        name = str(args.get("name") or source.rstrip("/").split("/")[-1].removesuffix(".git"))
        if not _NAME.match(name):
            raise _refuse("bad_args", f"{name!r} is no valid repo name.")
        owner, _own = _new_repo_owner(access, args)
        items = [str(i) for i in (args.get("items") or ("issues", "pull_requests", "labels", "milestones", "releases"))]
        bad = [i for i in items if i not in IMPORT_ITEMS]
        if bad:
            raise _refuse("bad_args", f"Unknown items {bad}; use {', '.join(IMPORT_ITEMS)}.")
        if service == "git" and set(items) - {"wiki", "lfs"}:
            items = [i for i in items if i in ("wiki", "lfs")]  # a plain git source has no issues to bring
        body = {"clone_addr": source, "repo_owner": owner, "repo_name": name, "service": service,
                "private": bool(args.get("private", True)), "mirror": False,
                "description": f"Imported from {source} by Forgebrücke", **{i: True for i in items}}
        return WritePlan(TOOL, f"{owner}/{name}", (Request("POST", "/repos/migrate", body),),
                         f"imports {source} as {owner}/{name} ({'private' if body['private'] else 'public'}) with the "
                         f"code{', ' + ', '.join(items) if items else ''}; the source is not changed",
                         json.dumps(body, indent=1), extra={"action": "import", "repo": f"{owner}/{name}",
                                                             "items": items, "source": source})

    def _plan_mirror(access, repo, args):
        target = _mod("access").parse_repo(args.get("target"))
        access.check_write(repo, "write")
        info = access.reader().get(f"/repos/{repo}").data or {}
        if not (info.get("permissions") or {}).get("admin"):
            link = access.collaboration_url(repo)
            right = _mod("access").right_from(info.get("permissions"))
            raise _mod("access").Refusal(
                "needs_admin", f"Push mirrors need admin on {repo}. Hermes' account has {right} there. "
                               f"Set the mirror up in Forgejo or grant admin: {link}", link=link)
        tinfo = access.writer().get(f"/repos/{target}").data or {}
        if not (tinfo.get("permissions") or {}).get("push"):
            raise _refuse("not_allowed", f"Hermes' account cannot push to {target}, so a mirror could not either.")
        me = (access.writer().get("/user").data or {}).get("login") or ""
        address = tinfo.get("clone_url") or f"{access.settings.url.rstrip('/')}/{target}.git"
        body = {"remote_address": address, "remote_username": me, "remote_password": "<Hermes' token>",
                "interval": "8h0m0s", "sync_on_commit": True}
        return WritePlan(TOOL, repo, (Request("POST", f"/repos/{repo}/push_mirrors", body),
                                      Request("POST", f"/repos/{repo}/push_mirrors-sync", {})),
                         f"a push mirror {repo} → {target} on this Forgejo, synced now and on every commit. "
                         "Forgejo stores Hermes' token as the mirror password",
                         json.dumps(body, indent=1), extra={"action": "mirror", "repo": repo, "target": target})

    def _plan_template(access, args):
        template = _mod("access").parse_repo(args.get("repo"))
        info = access.reader().get(f"/repos/{template}").data or {}
        if not info.get("template"):
            raise _refuse("not_template", f"{template} is not marked as a template repo (Settings → Template).")
        name = str(args.get("name") or "")
        if not _NAME.match(name):
            raise _refuse("bad_args", "Give the new repo's name (name).")
        owner, own = _new_repo_owner(access, args)
        link = f"{access.settings.url.rstrip('/')}/repo/create?template_id={info.get('id')}"
        missing = _scopes_missing_to_create(access) if own else ()
        if missing:
            raise _refuse("dark", f"Hermes' token lacks the scope {', '.join(missing)}, so it cannot create a repo "
                                  "in its own account. Create a token that includes it, or use the web form: "
                                  f"{link}", "token")
        body = {"owner": owner, "name": name, "private": bool(args.get("private", True)), "git_content": True,
                "protected_branch": True, "labels": True, "topics": True, "git_hooks": False, "webhooks": False,
                "avatar": False, "description": f"Created from {template} by Forgebrücke"}
        return WritePlan(TOOL, f"{owner}/{name}", (Request("POST", f"/repos/{template}/generate", body),),
                         f"creates {owner}/{name} from the template {template}, with its files, labels and branch "
                         f"protection (web form instead: {link})", json.dumps(body, indent=1),
                         extra={"action": "create_from_template", "repo": f"{owner}/{name}", "template": template,
                                "web_link": link})

    def _poll_import(access, p, err) -> dict:
        """The migrate call outlived the timeout: watch for the repo, then report what is there so far."""
        repo = p.extra["repo"]
        ForgejoError = _mod("client").ForgejoError
        for _ in range(POLL_TRIES):
            try:
                info = access.writer().get(f"/repos/{repo}").data or {}
            except ForgejoError:
                info = None
            if info and not info.get("empty", False):
                out = _after_create(access, p, info)
                out["message"] = ("The import took longer than one request; the repo is there now. Counts are what "
                                  "has come over so far: run forgejo_repos action=overview again in a minute.")
                return out
            _sleep(POLL_WAIT_S)
        return {"ok": False, "sent": True, "error": err.kind, "message": err.message + f" {repo} has not appeared "
                "yet. Forgejo may still be importing. Check it in a few minutes."}

    def _after_create(access, p, last) -> dict:
        """What came over, read back from the forge (never from the request)."""
        repo, action = p.extra["repo"], p.extra["action"]
        reader = access.writer()
        ForgejoError = _mod("client").ForgejoError
        if action == "mirror":
            mirrors = reader.get(f"/repos/{repo}/push_mirrors").data or []
            return {"ok": True, "sent": True, "mirrors": [{"remote": m.get("remote_address"),
                                                           "last_update": m.get("last_update"),
                                                           "last_error": m.get("last_error") or ""} for m in mirrors],
                    "message": f"Push mirror {repo} → {p.extra['target']} set and synced once. Check the target's "
                               "branch head with forgejo_get kind=branch."}
        out = {"ok": True, "sent": True, "repo": repo, "url": (last or {}).get("html_url")}
        if action == "create_from_template":
            try:
                rules = reader.get(f"/repos/{repo}/branch_protections").data or []
                out["protection"] = [r.get("rule_name") or r.get("branch_name") for r in rules]
            except ForgejoError as e:
                out["protection"] = f"not readable ({e.status})"
            out["web_link"] = p.extra["web_link"]
            out["message"] = ("Created. Branch protection on the new repo: "
                              + (", ".join(out["protection"]) if isinstance(out["protection"], list) and out["protection"]
                                 else "none came over") + ".")
            return out
        came = {}
        for item, path in (("issues", "issues?type=issues&state=all"), ("pull_requests", "pulls?state=all"),
                           ("labels", "labels"), ("milestones", "milestones?state=all"), ("releases", "releases")):
            if item not in p.extra["items"]:
                continue
            try:
                resp = reader.get(f"/repos/{repo}/{path.split('?')[0]}",
                                  dict(x.split("=") for x in path.split("?")[1].split("&")) if "?" in path else None)
                total = (resp.headers or {}).get("x-total-count") or (resp.headers or {}).get("X-Total-Count")
                came[item] = int(total) if total else len(resp.data or [])
            except ForgejoError as e:
                came[item] = f"not readable ({e.status})"
        out.update(came_over=came, asked=p.extra["items"],
                   message=f"Imported {p.extra['source']} as {repo}. What came over is counted on Forgejo. Compare "
                           "it with the source before you switch.",
                   changeover=["forgejo_repos action=mirror to keep the old host in sync while you switch",
                               f"git remote set-url origin {access.settings.url.rstrip('/')}/{repo}.git (in your working copy)"])
        return out

    return ReposSpec()


# --- reads -----------------------------------------------------------------------------------------------

def _days_since(stamp: Any, now: float) -> Optional[int]:
    try:
        when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return int((now - when.timestamp()) // 86400)


def _listed(access) -> "tuple[list, int]":
    """The repos an overview or audit without ``repo`` covers, cut at MAX_REPOS, and how many there are: the
    rules' repos, else the ones the agent account sees."""
    names = [r.repo_glob for r in access.settings.writes if "*" not in r.repo_glob and "?" not in r.repo_glob]
    if not names:
        names = [r["repo"] for r in access.repos() if not r.get("archived")]
    if not names:
        raise _refuse("bad_args", "Name a repo (repo=owner/name). Hermes' account sees none.")
    return names[:MAX_REPOS], len(names)


def _each(names: list, one) -> list:
    """One row per repo. A repo that cannot be read becomes ``{repo, error, message}``; the rest still answer."""
    Refusal, ForgejoError = _mod("access").Refusal, _mod("client").ForgejoError
    rows = []
    for name in names:
        try:
            rows.append(one(name))
        except Refusal as r:
            rows.append({"repo": name, "error": r.code, "message": r.message})
        except ForgejoError as e:
            rows.append({"repo": name, "error": e.kind, "message": e.message})
    return rows


def _protection(access, repo: str, base: str) -> dict:
    """The protection rule of ``base`` in short. Forgejo shows it to a repo admin only."""
    try:
        rules = access.reader().get(f"/repos/{repo}/branch_protections").data or []
    except _mod("client").ForgejoError as e:
        return {"branch": base, "unknown": f"could not be read ({e.status})"}
    rule = next((r for r in rules if (r.get("rule_name") or r.get("branch_name")) == base), None)
    if rule is None:
        return {"branch": base, "protected": False}
    return {"branch": base, "protected": True, "direct_push": bool(rule.get("enable_push")),
            "required_checks": rule.get("status_check_contexts") or [] if rule.get("enable_status_check") else [],
            "approvals": rule.get("required_approvals") or 0,
            "mergers": rule.get("merge_whitelist_usernames") if rule.get("enable_merge_whitelist") else "anyone with write"}


def overview(access, repo: str) -> dict:
    right = access.check_read(repo)
    info = access.reader().get(f"/repos/{repo}").data or {}
    base = info.get("default_branch") or "main"
    if right == "admin":
        protection = _protection(access, repo, base)
    else:
        protection = {"branch": base, "unknown": f"needs admin (the account has {right})"}
    return {"repo": repo, "owner": (info.get("owner") or {}).get("login"), "private": bool(info.get("private")),
            "your_access": right, "collaboration_url": access.collaboration_url(repo),
            "default_branch": base, "protection": protection,
            "open_prs": info.get("open_pr_counter"), "open_issues": info.get("open_issues_count"),
            "last_activity": info.get("updated_at"), "url": info.get("html_url")}


SHORT_ROW = ("repo", "owner", "private", "your_access", "protection", "open_prs", "url")


def short_overview(access, seen: dict, repo: str) -> dict:
    """One row of an overview over many repos, from the account's repo list (``seen``, by lowercase name). Only
    an admin repo, or one the list does not hold, costs its own reads."""
    entry = seen.get(repo.lower())
    if entry is None or entry["right"] == "admin":
        full = overview(access, repo)
        return {k: full[k] for k in SHORT_ROW}
    return {"repo": entry["repo"], "owner": entry["owner"], "private": entry["private"], "your_access": entry["right"],
            "protection": {"unknown": "needs admin"}, "open_prs": entry["open_prs"], "url": entry["url"]}


def audit(access, repo: str, *, now: Optional[float] = None) -> dict:
    """Read-only. At most MAX_FINDINGS, most severe first; checks that need admin say so instead."""
    now = time.time() if now is None else now
    right = access.check_read(repo)
    reader = access.reader()
    ForgejoError = _mod("client").ForgejoError
    info = reader.get(f"/repos/{repo}").data or {}
    url = info.get("html_url") or ""
    base = info.get("default_branch") or "main"
    findings, not_checked = [], []
    admin = right == "admin"
    if not admin:
        not_checked.append(f"branch protection, deploy keys and webhooks need admin on {repo} (the account has "
                           f"{right}): {access.collaboration_url(repo)}")

    def add(rule, severity, title, detail, link, fix):
        findings.append({"rule": rule, "severity": severity, "repo": repo, "title": title, "detail": detail,
                         "link": link, "fix": fix})

    rules = None
    try:
        rules = reader.get(f"/repos/{repo}/branch_protections").data or [] if admin else None
    except ForgejoError as e:
        not_checked.append(f"branch protection (HTTP {e.status})")
    if rules is not None:
        rule = next((r for r in rules if (r.get("rule_name") or r.get("branch_name")) == base), None)
        if rule is None:
            add("unprotected_default", 3, f"`{base}` is not protected", "Anyone with write access, agents included, "
                "can push to it directly.", f"{url}/settings/branches",
                f"forgejo_repos action=preset repo={repo} preset=solo-with-agents")
        else:
            if not rule.get("enable_status_check") or not rule.get("status_check_contexts"):
                add("no_required_checks", 2, f"`{base}` merges without required checks", "A pull request can be "
                    "merged while its checks are red or missing.", f"{url}/settings/branches",
                    "Turn on required checks for the contexts your CI posts (for example devops/tests).")
            if not rule.get("required_approvals"):
                add("approvals_off", 1, f"`{base}` needs no approval", "A pull request can be merged without anyone "
                    "else looking at it. Fine when you work alone with agents.", f"{url}/settings/branches",
                    f"For two people: forgejo_repos action=preset repo={repo} preset=team-of-two")
    try:
        keys = reader.get(f"/repos/{repo}/keys").data or [] if admin else []
        for k in keys:
            age = _days_since(k.get("created_at"), now)
            if not k.get("read_only", True):
                add("deploy_key_write", 2, f"Deploy key {k.get('title')!r} can push", "A machine holding this key can "
                    "change the code.", f"{url}/settings/keys", "Replace it with a read-only key unless it deploys by push.")
            elif age is not None and age > STALE_DAYS:
                add("deploy_key_stale", 1, f"Deploy key {k.get('title')!r} is {age} days old", "Old keys are often "
                    "forgotten on machines nobody uses.", f"{url}/settings/keys", "Remove it if nothing uses it.")
    except ForgejoError as e:
        not_checked.append(f"deploy keys (needs admin; HTTP {e.status})")
    try:
        hooks = reader.get(f"/repos/{repo}/hooks").data or [] if admin else []
        for h in hooks:
            age = _days_since(h.get("updated_at"), now)
            target = (h.get("config") or {}).get("url") or h.get("type")
            if not h.get("active"):
                add("webhook_inactive", 1, f"Webhook to {target} is switched off", "An inactive hook is easy to "
                    "switch on again by mistake.", f"{url}/settings/hooks", "Delete it if it is not needed.")
            elif age is not None and age > STALE_DAYS:
                add("webhook_stale", 1, f"Webhook to {target} unchanged for {age} days", "Check that the "
                    "receiver still exists.", f"{url}/settings/hooks", "Delete it if the receiver is gone.")
    except ForgejoError as e:
        not_checked.append(f"webhooks (needs admin; HTTP {e.status})")
    if not info.get("private") and not info.get("template") and not info.get("fork"):
        add("public", 2, f"{repo} is public", "Anyone on the internet can read the code and the issues.",
            f"{url}/settings", "If that is not meant, make it private (Settings → Danger zone → Make private).")
    findings.sort(key=lambda f: -f["severity"])
    return {"repo": repo, "findings": findings[:MAX_FINDINGS], "more": max(0, len(findings) - MAX_FINDINGS),
            "not_checked": not_checked}


def handle(access, args: dict) -> dict:
    action = str(args.get("action") or "")
    if action not in ("overview", "audit"):
        raise _refuse("bad_args", f"action is one of {', '.join(ACTIONS)}.")
    if action == "audit" and not access.settings.audit_enabled:
        raise _refuse("off", "The security audit is off. The user switches it on with Security audit on the "
                             "Forgebrücke page.", "audit_enabled")
    key, one = ("repos", overview) if action == "overview" else ("audits", audit)
    if args.get("repo"):
        out = {"ok": True, key: [one(access, _mod("access").parse_repo(args.get("repo")))]}
    else:
        names, total = _listed(access)
        if action == "overview":
            seen = {r["repo"].lower(): r for r in access.repos()}
            rows = _each(names, lambda name: short_overview(access, seen, name))
        else:
            rows = _each(names, lambda name: audit(access, name))
        out = {"ok": True, key: rows, "shown": len(rows), "total": total, "has_more": total > len(rows)}
        if out["has_more"]:
            out["message"] = f"{total - len(rows)} more repos are not shown. Pass repo to read one of them."
    return _mod("tools.write").noted(out) if action == "audit" else out


def to_json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
