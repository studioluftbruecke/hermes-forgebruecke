"""forgejo_flows: read, check, write, run, pause and resume flows; show one PR's gate chain.

Actions (named here and in the tool description, so the agent never has to search the source):

- ``get`` (repo): the flows that run on a repo, where each comes from, and any invalid file with its errors.
- ``validate`` (text, or repo): check a flow file before it is used.
- ``template`` (name, repo): one of ``solo-with-agents``, ``one-review-then-me``, ``staging-first`` as YAML.
- ``set`` (repo, text or template, flow, target ``repo``|``local``): ``repo`` writes the flow file on a new
  branch and opens a pull request into the default branch (never a push to it); ``local`` saves it as this
  profile's override file. Asks the user first (Hermes' approval), refused in unattended runs.
- ``run`` (repo, number): one tick of the engine for one pull request, now.
- ``status`` (repo, number): the gate chain of one pull request.
- ``pause`` (flow, repo optional): stop a flow; ``resume`` starts it again and asks the user first.
"""
from __future__ import annotations

import base64
import importlib
import json
import re
from typing import Any, Callable, Optional

ACTIONS = ("get", "validate", "template", "set", "run", "status", "pause", "resume")
GATED = ("set", "resume")
TOOL = "forgejo_flows"


def _mod(name: str):
    pkg = (__package__ or "").rpartition(".")[0] or __package__
    return importlib.import_module(f".{name}", pkg)


def _refuse(code: str, message: str, setting: str = ""):
    return _mod("access").Refusal(code, message, setting)


def _flow_id(args: dict) -> str:
    fid = str(args.get("flow") or "").strip()
    if fid and not _mod("models").FLOW_ID_RE.match(fid):
        raise _refuse("bad_args", f"{fid!r} is not a flow id (lowercase letters, digits, dashes).")
    return fid


def _number(args: dict) -> int:
    try:
        n = int(args.get("number"))
        if n > 0:
            return n
    except (TypeError, ValueError):
        pass
    raise _refuse("bad_args", "Give the pull request number (number).")


def _flow_text(access, args: dict) -> "tuple[str, str, Any]":
    """``(flow_id, yaml_text, data)`` from ``text`` or ``template``; ``data`` is the parsed flow (a template's
    own data, so validating it needs no YAML library)."""
    models, templates = _mod("models"), _mod("flows.templates")
    repo = _mod("access").parse_repo(args.get("repo"))
    text = str(args.get("text") or "")
    if text.strip():
        data = models.parse_flow_text(text)
        flows = models.raw_flows(data)
        if len(flows) != 1 or not isinstance(flows[0], dict):
            raise _refuse("bad_args", "set takes one flow per call.")
        fid = str(flows[0].get("flow") or "")
        return fid, (text if text.endswith("\n") else text + "\n"), flows[0]
    name = str(args.get("template") or "").strip()
    if not name:
        raise _refuse("bad_args", "Give the flow as text (YAML) or a template name: "
                                  + ", ".join(templates.TEMPLATES) + ".")
    try:
        data = templates.render(name, repo, access.settings.default_review_profile, str(args.get("owner") or ""),
                                _flow_id(args) or None)
    except KeyError:
        raise _refuse("bad_args", f"No template {name!r}; choose one of {', '.join(templates.TEMPLATES)}.") from None
    header = f"Forgebrücke flow, from the template {name}. Docs: the plugin's README, Flows."
    return data["flow"], models.flow_to_yaml(data, header), data


def _validated(access, data: Any, repo: str, *, only_repo: str = ""):
    sources, models = _mod("flows.sources"), _mod("models")
    visible = sources.visible_repos(access.reader())
    raw = {**data, "repos": [only_repo]} if only_repo and isinstance(data, dict) else data
    flow, errors, warnings = models.validate_flow(raw, default_review_profile=access.settings.default_review_profile,
                                                  visible_repos=visible, source="proposed")
    item = sources.Loaded("proposed", flow, errors, warnings, only_repo)
    if item.flow is None:
        raise _refuse("invalid_flow", "The flow is invalid, so nothing was written: " + "; ".join(item.errors))
    if not item.flow.applies_to(repo):
        raise _refuse("invalid_flow", f"The flow's repos do not include {repo}.")
    return item


# --- gated actions: planned, approved in Hermes, then executed --------------------------------------------

def make_spec(store_factory: Callable[[], Any]):
    write = _mod("tools.write")
    Request, WritePlan = write.Request, write.WritePlan

    class FlowsSpec:
        @staticmethod
        def gated(args: Any) -> bool:
            return isinstance(args, dict) and str(args.get("action") or "") in GATED

        @staticmethod
        def plan(access, args: dict):
            action = str(args.get("action"))
            if action == "resume":
                fid = _flow_id(args)
                if not fid:
                    raise _refuse("bad_args", "Give the flow id to resume (flow).")
                repo = str(args.get("repo") or "").strip()
                key = f"{fid}@{_mod('access').parse_repo(repo)}" if repo else fid
                return WritePlan(TOOL, repo or "(every repo)", (), f"resumes the flow {fid}"
                                 + (f" on {repo}" if repo else "") + "; its gates run unattended again",
                                 "", extra={"action": "resume", "key": key})
            repo = _mod("access").parse_repo(args.get("repo"))
            rule = access.settings.explicit_rule(repo)
            fid, text, data = _flow_text(access, args)
            target = str(args.get("target") or ("repo" if access.right(repo) in _mod("access").WRITE_RIGHTS
                                                 else "local"))
            if target not in ("repo", "local"):
                raise _refuse("bad_args", "target is repo or local.")
            hint = "" if (rule and fid in rule.flows) else (
                f" Note: the rule for {repo} does not list {fid} in flows yet, so the flow will not run until "
                f"the user adds it (writes setting).")
            if target == "local":
                _validated(access, data, repo)
                return WritePlan(TOOL, repo, (), f"saves the flow {fid} as this profile's override file.{hint}", text,
                                 extra={"action": "set", "target": "local", "flow": fid, "text": text})
            access.check_write(repo, "write")
            _validated(access, data, repo, only_repo=repo)
            info = access.reader().get(f"/repos/{repo}").data or {}
            base = info.get("default_branch") or "main"
            path = access.settings.flows_path
            branch = f"forgebruecke/flow-{fid}"
            existing = None
            ForgejoError = _mod("client").ForgejoError
            try:
                existing = (access.reader().get(f"/repos/{repo}/contents/{path}", {"ref": base}).data or {}).get("sha")
            except ForgejoError as e:
                if e.status != 404:
                    raise
            body = {"content": base64.b64encode(text.encode("utf-8")).decode("ascii"), "branch": base,
                    "new_branch": branch, "message": f"Forgebrücke: flow {fid}"}
            if existing:
                body["sha"] = existing
            pr_body = (f"Adds the Forgebrücke flow `{fid}` as `{path}`. Merging it is your call; the flow runs once "
                       f"the rule for {repo} lists `{fid}` in flows.\n\n```yaml\n{text}```" + write.FOOTER)
            reqs = (Request("PUT" if existing else "POST", f"/repos/{repo}/contents/{path}", body),
                    Request("POST", f"/repos/{repo}/pulls", {"head": branch, "base": base,
                                                            "title": f"Forgebrücke flow {fid}", "body": pr_body}))
            return WritePlan(TOOL, repo, reqs, f"the flow file {path} on a new branch {branch}, and a pull request "
                                              f"{branch} → {base} (never a push to {base}).{hint}", text,
                             extra={"action": "set", "target": "repo", "flow": fid})

        @staticmethod
        def execute(access, p) -> dict:
            store = store_factory()
            if p.extra.get("action") == "resume":
                return {"ok": True, "sent": False, "paused": store.set_paused(p.extra["key"], False),
                        "message": f"Resumed {p.extra['key']}."}
            if p.extra.get("target") == "local":
                path = store.write_override(p.extra["flow"], p.extra["text"])
                return {"ok": True, "sent": False, "saved": str(path.name), "flow": p.extra["flow"],
                        "message": "Saved as this profile's override file; it wins over a repo file of the same id."}
            writer = access.writer()
            ForgejoError = _mod("client").ForgejoError
            try:
                writer.request(p.requests[0].method, p.requests[0].path, json_body=p.requests[0].body)
                pr = writer.request(p.requests[1].method, p.requests[1].path, json_body=p.requests[1].body).data or {}
            except ForgejoError as e:
                return {"ok": False, "sent": True, "error": e.kind, "status": e.status, "message": e.message}
            return {"ok": True, "sent": True, "number": pr.get("number"), "url": pr.get("html_url"),
                    "message": "The flow file waits in a pull request; merging it is the user's click."}

    return FlowsSpec()


# --- the other actions -------------------------------------------------------------------------------

def gate_chain(st: dict) -> list:
    return [{"gate": g["gate_key"], "type": g["type"], "state": g["state"], "detail": g.get("detail", ""),
             "head": str(g.get("head_sha") or "")[:12], "carried_from": str(g.get("carried_from") or "")[:12],
             "attempts": g.get("attempts", 0), **({"findings": g["evidence"]["findings"]}
                                                    if (g.get("evidence") or {}).get("findings") else {}),
             **({"flags": g["evidence"]["flags"]} if (g.get("evidence") or {}).get("flags") else {})}
            for g in st.get("gates") or []]


def handle(access, store, args: dict, *, engine_factory: Callable[[Any, Any], Any]) -> dict:
    action = str(args.get("action") or "")
    if action not in ACTIONS:
        raise _refuse("bad_args", f"action is one of {', '.join(ACTIONS)}.")
    models, sources, templates = _mod("models"), _mod("flows.sources"), _mod("flows.templates")
    acc = _mod("access")
    if action == "template":
        name = str(args.get("template") or args.get("name") or "").strip()
        if not name:
            return {"ok": True, "templates": templates.names()}
        repo = acc.parse_repo(args.get("repo") or "owner/repo")
        fid, text, _data = _flow_text(access, {**args, "template": name, "repo": repo})
        return {"ok": True, "flow": fid, "yaml": text, "next": "forgejo_flows action=set with this text, or edit it first."}
    if action == "validate":
        text = str(args.get("text") or "")
        if text.strip():
            visible = sources.visible_repos(access.reader())
            items = sources.parse_file(text, "proposed", default_review_profile=access.settings.default_review_profile,
                                       visible=visible)
            return {"ok": all(i.flow is not None for i in items),
                    "flows": [{"valid": i.flow is not None, "flow": i.flow.to_dict() if i.flow else None,
                               "errors": i.errors, "warnings": i.warnings} for i in items]}
        repo = acc.parse_repo(args.get("repo"))
        plan = sources.plan(access.reader(), access.settings, store, only_repo=repo)
        return {"ok": not any(i.flow is None for i in plan.loaded),
                "files": [{"source": i.source, "valid": i.flow is not None, "errors": i.errors, "warnings": i.warnings}
                          for i in plan.loaded], "notes": plan.notes}
    if action == "get":
        repo = acc.parse_repo(args.get("repo"))
        access.check_read(repo)
        plan = sources.plan(access.reader(), access.settings, store, only_repo=repo)
        rule = access.settings.explicit_rule(repo)
        return {"ok": True, "repo": repo, "rule_flows": list(rule.flows) if rule else [],
                "active": [f.to_dict() for r, f in plan.active],
                "files": [{"source": i.source, "valid": i.flow is not None, "flow": i.flow.id if i.flow else None,
                           "errors": i.errors, "warnings": i.warnings} for i in plan.loaded],
                "notes": plan.notes, "engine": store.engine()}
    if action == "status":
        repo, n = acc.parse_repo(args.get("repo")), _number(args)
        access.check_read(repo)
        st = store.load_pr(repo, n)
        if st is None:
            return {"ok": True, "repo": repo, "number": n, "gates": [],
                    "message": "No flow has run on this pull request yet. forgejo_flows action=run starts one tick."}
        return {"ok": True, "repo": repo, "number": n, "flow": st.get("flow"), "head": str(st.get("head_sha"))[:12],
                "closed": st.get("closed"), "merged": st.get("merged"), "gates": gate_chain(st),
                "history": st.get("history", [])[-5:], "url": st.get("url")}
    if action == "run":
        repo, n = acc.parse_repo(args.get("repo")), _number(args)
        access.check_read(repo)
        report = engine_factory(access, store).tick(only=(repo, n))
        st = store.load_pr(repo, n)
        return {"ok": report.get("ok", False), "busy": report.get("busy", False), "notes": report.get("notes", []),
                "errors": report.get("errors", []), "writes": report.get("writes", 0), "dry_run": report.get("dry_run"),
                "gates": gate_chain(st) if st else [], "message": report.get("message", "")}
    if action == "pause":
        fid = _flow_id(args)
        if not fid:
            raise _refuse("bad_args", "Give the flow id to pause (flow).")
        repo = str(args.get("repo") or "").strip()
        key = f"{fid}@{acc.parse_repo(repo)}" if repo else fid
        return {"ok": True, "paused": store.set_paused(key, True),
                "message": f"Paused {key}. Running reviews finish; nothing new starts. resume asks the user first."}
    raise _refuse("bad_args", f"{action} needs the user's approval; it runs through the approval gate.")


SCHEMA = {
    "name": TOOL,
    "description": (
        "Flows: how a change travels to the target branch (agent review, checks, an optional staging step, then the "
        "user's merge). action=get (repo): the flows on a repo and any invalid flow file; validate (text or repo); "
        "template (template = solo-with-agents | one-review-then-me | staging-first, repo): the YAML to start "
        "from; set (repo, text or template, flow, target = repo | local): repo opens a pull request with the "
        "flow file, local saves this profile's override; asks the user first; run (repo, number): one engine "
        "tick for that pull request now; status (repo, number): its gate chain; pause (flow, repo optional); "
        "resume (asks first). A flow runs on a repo only when that repo's rule lists the flow id in flows. "
        "Gate types: review (by = a Hermes profile), check (contexts), promote (to = an unprotected branch, "
        "never main), notify (who), human (who; always last). Flows never merge main. See the skill "
        "forgebruecke:forgebruecke."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "repo": {"type": "string", "description": "owner/repo"},
        "number": {"type": "integer", "description": "pull request number (run, status)"},
        "flow": {"type": "string", "description": "flow id (pause, resume; set: the id for a template)"},
        "text": {"type": "string", "description": "a flow file, YAML (validate, set)"},
        "template": {"type": "string", "enum": ["solo-with-agents", "one-review-then-me", "staging-first"]},
        "target": {"type": "string", "enum": ["repo", "local"]},
        "owner": {"type": "string", "description": "template: who merges (default the repo owner)"},
    }, "required": ["action"]},
}


def to_json(result: Any) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
