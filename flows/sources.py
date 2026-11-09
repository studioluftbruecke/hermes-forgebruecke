"""Where flows come from (D10, provisional): the repo's own flow file, and per-profile override files.

- Repo file: ``<flows_path>`` (default ``.forgebruecke/flows.yml``) on the repo's default branch. It
  applies to that repo only, whatever its ``repos`` line says, so a file in one repo cannot start
  writes in another.
- Override file: ``<data_dir>/flows/overrides/<flow>.yml``. Same shape; its ``repos`` globs name where it
  applies; a flow id defined here wins over the repo file's flow of the same id.

A flow runs on a repo only when that repo's rule in ``writes`` lists the flow id in ``flows`` and Hermes'
Forgejo account has write access there: the flow file says how, the user's settings say where, and Forgejo
says whether the account may write at all (D27). The agent cannot widen the rule without the settings
guard asking the user.
"""
from __future__ import annotations

import base64
import importlib
from dataclasses import dataclass, field
from typing import Any, Optional


def _mod(name: str):
    pkg = (__package__ or "").rpartition(".")[0] or __package__
    return importlib.import_module(f".{name}", pkg)


@dataclass
class Loaded:
    source: str
    flow: Any = None              # FlowDef or None when invalid
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    repo: str = ""                # set for repo files: the only repo the flow may run on


@dataclass
class Plan:
    active: list = field(default_factory=list)    # [(repo, FlowDef)]
    loaded: list = field(default_factory=list)    # every Loaded, valid or not
    notes: list = field(default_factory=list)     # sentences for the desk and the doctor


def profile_warnings(flow) -> list:
    """A review gate names a Hermes profile; say so when that profile does not exist on this host."""
    known = _mod("profile").list_profiles()
    if not known or flow is None:
        return []
    return [f"the {g.key} gate names the profile {g.by}, which does not exist here (profiles: {', '.join(known)}); "
            "assign one you have, or create a reviewer profile from the desk's settings"
            for g in flow.gates if g.type == "review" and g.by not in known]


def parse_file(text: str, source: str, *, default_review_profile: str, visible: Optional[list] = None,
               only_repo: str = "") -> list:
    models = _mod("models")
    try:
        data = models.parse_flow_text(text)
    except ValueError as e:
        return [Loaded(source, None, [str(e)])]
    out = []
    for raw in models.raw_flows(data):
        warnings = []
        if only_repo and isinstance(raw, dict):
            named = raw.get("repos")
            if named not in (None, [only_repo], only_repo):
                warnings.append(f"a flow file in {only_repo} applies to {only_repo} only; its repos line was ignored")
            raw = {**raw, "repos": [only_repo]}
        flow, errors, w = models.validate_flow(raw, default_review_profile=default_review_profile,
                                               visible_repos=visible, source=source)
        out.append(Loaded(source, flow, errors, warnings + w + profile_warnings(flow), only_repo))
    if not out:
        out.append(Loaded(source, None, ["the file holds no flow"]))
    return out


def read_repo_file(reader, repo: str, path: str) -> Optional[str]:
    ForgejoError = _mod("client").ForgejoError
    try:
        data = reader.get(f"/repos/{repo}/contents/{path}").data or {}
    except ForgejoError as e:
        if e.status == 404:
            return None
        raise
    if not isinstance(data, dict) or data.get("type") not in (None, "file"):
        return None
    try:
        return base64.b64decode(data.get("content") or "").decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def visible_repos(reader) -> list:
    return [r.get("full_name") for r in reader.paginate("/user/repos", max_items=500).items if r.get("full_name")]


def plan(reader, settings, store, *, visible: Optional[list] = None, only_repo: str = "") -> Plan:
    """Which flows run on which repos now. Network: the repo list, one contents read per ruled repo. A ruled repo
    where the agent account has read access only runs no flow (its statuses and comments would be refused)."""
    result = Plan()
    read_only: set = set()
    if visible is None:
        items = reader.paginate("/user/repos", max_items=500).items
        visible = [r.get("full_name") for r in items if r.get("full_name")]
        read_only = {r.get("full_name") for r in items if not (r.get("permissions") or {}).get("push", True)}
    ruled = [r for r in visible if (settings.explicit_rule(r) is not None and settings.explicit_rule(r).flows)]
    for repo in [r for r in ruled if r in read_only]:
        result.notes.append(f"{repo}: Hermes' Forgejo account has read access only, so its flows do not run; give it "
                            f"write access in {settings.url.rstrip('/')}/{repo}/settings/collaboration")
    ruled = [r for r in ruled if r not in read_only]
    if only_repo:
        ruled = [r for r in ruled if r.lower() == only_repo.lower()]
    by_id: dict = {}
    for repo in ruled:
        try:
            text = read_repo_file(reader, repo, settings.flows_path)
        except Exception as e:  # one unreadable repo must not stop the others
            result.notes.append(f"{repo}: the flow file could not be read ({getattr(e, 'message', e)})")
            continue
        if text is None:
            continue
        for item in parse_file(text, f"repo:{repo}:{settings.flows_path}",
                               default_review_profile=settings.default_review_profile, only_repo=repo):
            result.loaded.append(item)
            if item.flow is not None:
                by_id.setdefault(item.flow.id, []).append(item)
    for path in store.override_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            result.notes.append(f"{path.name}: unreadable ({e})")
            continue
        for item in parse_file(text, f"local:{path.name}", default_review_profile=settings.default_review_profile):
            result.loaded.append(item)
            if item.flow is not None:
                by_id[item.flow.id] = [item]  # the override wins over a repo file of the same id
    for item in result.loaded:
        if item.flow is None:
            result.notes.append(f"{item.source}: invalid, so its flows do not run: {'; '.join(item.errors)}")
    seen = set()
    for fid, items in by_id.items():
        for item in items:
            for repo in ruled:
                if item.repo and item.repo.lower() != repo.lower():
                    continue
                if not item.flow.applies_to(repo):
                    continue
                rule = settings.explicit_rule(repo)
                if fid not in rule.flows:
                    continue
                if store.is_paused(fid, repo):
                    result.notes.append(f"{fid} on {repo} is paused")
                    continue
                if (repo, fid) not in seen:
                    seen.add((repo, fid))
                    result.active.append((repo, item.flow))
    for repo in ruled:
        rule = settings.explicit_rule(repo)
        for fid in rule.flows:
            if fid not in by_id:
                result.notes.append(f"{repo}: the rule lists flow {fid}, but no valid flow file defines it")
    return result
