"""Flow data shapes and their validator (PRODUCT.md § The flow model, § Data shapes).

- ``Gate(type, key, by, lens, max_fix_rounds, contexts, timeout_min, to, then, who, action, labels,
  creators, message)``: one step a change passes. ``type`` is one of ``GATE_TYPES``; ``key`` is the stage
  key (the type, or ``<type>-<n>`` when a type repeats), used in the status context
  ``forgebruecke/<flow>/<key>`` and mappable onto Hermes kanban's ``step_key``.
- ``FlowDef(id, name, repos, gates, on_failure, on_push, version, source)``: one flow. ``repos`` are
  ``owner/repo`` globs. ``on_push`` says what a new head commit resets: ``reset_failed`` (the product
  rule: every gate when none failed, else the failed gate and everything after it) or ``reset_all``.
  A higher ``version`` runs every gate of every open pull request again.
- ``GateRun``: the record of one gate on one head commit, stored by ``flows/state.py`` and rendered by
  the desk. ``state`` is one of ``RUN_STATES``; ``waiting`` is the human gate's state (the merge is his).

``validate_flow(raw, ...)`` returns ``(flow or None, errors, warnings)`` and never raises: an invalid flow
stops that repo's flows and says why, rather than running half of it (rule 6). Parsing YAML text is the
caller's job (``parse_flow_text``), because the test runner has no YAML library and Hermes does.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

GATE_TYPES = ("review", "check", "promote", "human", "notify")
RUN_STATES = ("pending", "running", "passed", "failed", "blocked", "skipped", "waiting", "error")
DONE_STATES = ("passed", "skipped")
ON_FAILURE = ("notify", "none")
ON_PUSH = ("reset_failed", "reset_all")
THEN = ("none", "verify_deploy")
PROTECTED_NAMES = ("main", "master")  # never a promote target, whatever the forge says
FLOW_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_CONTEXT_RE = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9._/*?-]{0,99}$")
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_REPO_GLOB_RE = re.compile(r"^[A-Za-z0-9_.*?\[\]-]+/[A-Za-z0-9_.*?\[\]-]+$")
GATE_KEYS = {
    "review": {"by", "lens", "max_fix_rounds", "timeout_min"},
    "check": {"contexts", "timeout_min", "creators"},
    "promote": {"to", "then", "contexts", "timeout_min"},
    "human": {"who", "action"},
    "notify": {"who", "message"},
}
DEFAULTS = {"review": {"lens": "correctness", "max_fix_rounds": 3, "timeout_min": 20},
            "check": {"timeout_min": 120}, "promote": {"then": "none", "timeout_min": 30},
            "human": {"action": "merge"}, "notify": {}}


@dataclass(frozen=True)
class Gate:
    type: str
    key: str
    by: str = ""
    lens: str = ""
    max_fix_rounds: int = 0
    contexts: tuple = ()
    timeout_min: int = 0
    to: str = ""
    then: str = "none"
    who: str = ""
    action: str = ""
    labels: tuple = ()
    creators: tuple = ()
    message: str = ""

    def context(self, flow_id: str) -> str:
        return f"forgebruecke/{flow_id}/{self.key}"

    def label(self) -> str:
        if self.type == "review":
            return f"review by {self.by}"
        if self.type == "check":
            return "checks " + ", ".join(self.contexts)
        if self.type == "promote":
            return f"promote to {self.to}"
        if self.type == "human":
            return f"{self.who or 'you'} merges"
        return f"notify {self.who}"

    def to_dict(self) -> dict:
        d: dict = {"type": self.type, "key": self.key}
        for k in sorted(GATE_KEYS[self.type]):
            v = getattr(self, k)
            d[k] = list(v) if isinstance(v, tuple) else v
        return d


@dataclass(frozen=True)
class FlowDef:
    id: str
    name: str
    repos: tuple
    gates: tuple
    on_failure: str = "notify"
    on_push: str = "reset_failed"
    version: int = 1
    source: str = ""

    def applies_to(self, repo: str) -> bool:
        return any(fnmatch.fnmatchcase(repo.lower(), g.lower()) for g in self.repos)

    def human(self) -> Optional[Gate]:
        return next((g for g in self.gates if g.type == "human"), None)

    def gate_hashes(self) -> list:
        """One hash per gate's definition, in order. A stored run belongs to the definition it ran under, so
        the engine runs a gate again when its hash differs from the one in the pull request's state."""
        return [hashlib.sha256(json.dumps(g.to_dict(), sort_keys=True).encode("utf-8")).hexdigest()[:16]
                for g in self.gates]

    def to_dict(self) -> dict:
        return {"flow": self.id, "name": self.name, "repos": list(self.repos), "gates": [g.to_dict() for g in self.gates],
                "on_failure": self.on_failure, "on_push": self.on_push, "version": self.version, "source": self.source}


@dataclass
class GateRun:
    id: str
    flow: str
    repo: str
    pr: int
    head_sha: str
    gate_index: int
    gate_key: str
    type: str
    state: str = "pending"
    attempts: int = 0
    evidence: dict = field(default_factory=dict)
    started_at: Optional[int] = None
    ended_at: Optional[int] = None
    detail: str = ""
    carried_from: str = ""
    posted: str = ""  # the status last sent to the forge for this run: "<sha12> <state> <description>"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GateRun":
        known = {k: d[k] for k in cls.__dataclass_fields__ if k in d}
        return cls(**known)


def run_id(repo: str, pr: int, sha: str, key: str) -> str:
    """The idempotency key of one gate run: ``repo#pr@sha12:stage``."""
    return f"{repo}#{pr}@{sha[:12]}:{key}"


# --- parsing and validation -------------------------------------------------------------------------

def parse_flow_text(text: str) -> Any:
    """YAML (or JSON, a subset) to Python data. Raises ValueError with a readable message."""
    try:
        import hermes_yaml as yaml  # noqa: F401  (what Hermes reads its own YAML with)
    except ImportError:
        try:
            import yaml  # type: ignore
        except ImportError:
            yaml = None
    if yaml is not None:
        try:
            return yaml.safe_load(text)
        except Exception as e:  # yaml.YAMLError and friends
            raise ValueError(f"the flow file is not valid YAML: {e}") from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"the flow file is not valid JSON (no YAML library here): {e}") from None


def raw_flows(data: Any) -> list:
    """A file holds one flow (a mapping with ``flow``) or several (``flows: [...]``, or a list)."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("flows"), list):
        return data["flows"]
    if isinstance(data, dict):
        return [data]
    return []


def _int(v: Any, where: str, errors: list, lo: int, hi: int) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        errors.append(f"{where} must be a whole number")
        return lo
    if not lo <= v <= hi:
        errors.append(f"{where} must be between {lo} and {hi}")
    return v


def _strs(v: Any, where: str, errors: list, pattern: re.Pattern) -> tuple:
    if v is None:
        return ()
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and pattern.match(x) for x in v):
        errors.append(f"{where} must be a list of names")
        return ()
    return tuple(v)


def _gate(i: int, raw: Any, errors: list, warnings: list, *, flow_id: str) -> Optional[dict]:
    where = f"gates[{i}]"
    if isinstance(raw, dict) and len(raw) == 1 and next(iter(raw)) in GATE_TYPES:
        gtype, body = next(iter(raw.items()))  # the PRODUCT.md shape: - review: {by: devops}
        body = body if isinstance(body, dict) else {}
    elif isinstance(raw, dict) and "type" in raw:
        gtype, body = raw.get("type"), {k: v for k, v in raw.items() if k not in ("type", "key")}
    elif isinstance(raw, str) and raw in GATE_TYPES:
        gtype, body = raw, {}
    else:
        name = next(iter(raw)) if isinstance(raw, dict) and raw else raw
        errors.append(f"{where}: unknown gate {name!r}; a gate is one of {', '.join(GATE_TYPES)}")
        return None
    if gtype not in GATE_TYPES:
        errors.append(f"{where}: unknown gate {gtype!r}; a gate is one of {', '.join(GATE_TYPES)}")
        return None
    unknown = sorted(set(body) - GATE_KEYS[gtype])
    if unknown:
        errors.append(f"{where} ({gtype}): unknown keys {', '.join(unknown)}; allowed: {', '.join(sorted(GATE_KEYS[gtype]))}")
    g = {"type": gtype, **DEFAULTS[gtype], **{k: v for k, v in body.items() if k in GATE_KEYS[gtype]}}
    w = f"{where} ({gtype})"
    if gtype == "review":
        if not isinstance(g.get("by"), str) or not _PROFILE_RE.match(g.get("by") or ""):
            errors.append(f"{w}: name the Hermes profile that reviews (by: <profile name>)")
        g["max_fix_rounds"] = _int(g["max_fix_rounds"], f"{w}.max_fix_rounds", errors, 1, 10)
        g["timeout_min"] = _int(g["timeout_min"], f"{w}.timeout_min", errors, 1, 240)
        if not isinstance(g.get("lens"), str) or not g["lens"].strip():
            errors.append(f"{w}.lens must be a word such as correctness")
    elif gtype == "check":
        g["contexts"] = _strs(g.get("contexts"), f"{w}.contexts", errors, _CONTEXT_RE)
        if not g["contexts"]:
            errors.append(f"{w}: name the status contexts to wait for (contexts: [devops/tests])")
        if any(c.startswith(f"forgebruecke/{flow_id}/") for c in g["contexts"]):
            errors.append(f"{w}: a check cannot wait for this flow's own statuses (forgebruecke/{flow_id}/...)")
        g["creators"] = _strs(g.get("creators"), f"{w}.creators", errors, _USER_RE)
        g["timeout_min"] = _int(g["timeout_min"], f"{w}.timeout_min", errors, 1, 1440)
    elif gtype == "promote":
        to = g.get("to")
        if not isinstance(to, str) or not _BRANCH_RE.match(to):
            errors.append(f"{w}: name the branch to promote into (to: staging)")
        elif to.lower() in PROTECTED_NAMES:
            errors.append(f"{w}: to: {to} is not allowed. A flow never merges into {to}; that merge is yours")
        if g.get("then") not in THEN:
            errors.append(f"{w}.then must be one of {', '.join(THEN)}")
        g["contexts"] = _strs(g.get("contexts"), f"{w}.contexts", errors, _CONTEXT_RE)
        if g.get("then") == "verify_deploy" and not g["contexts"]:
            errors.append(f"{w}: then: verify_deploy needs contexts, the deploy statuses to wait for")
        g["timeout_min"] = _int(g["timeout_min"], f"{w}.timeout_min", errors, 1, 1440)
    elif gtype in ("human", "notify"):
        who = g.get("who")
        if who is not None and (not isinstance(who, str) or not _USER_RE.match(who.lstrip("@"))):
            errors.append(f"{w}.who must be a user name")
        g["who"] = (who or "").lstrip("@")
        if gtype == "human" and g.get("action") != "merge":
            errors.append(f"{w}.action must be merge")
        if gtype == "notify" and not g["who"]:
            errors.append(f"{w}: name who to notify (who: <user>)")
        if gtype == "notify":
            g["message"] = str(g.get("message") or "")[:500]
    return g


def validate_flow(raw: Any, *, default_review_profile: str = "", visible_repos: Optional[Iterable[str]] = None,
                  source: str = "") -> "tuple[Optional[FlowDef], list, list]":
    errors: list = []
    warnings: list = []
    if not isinstance(raw, dict):
        return None, ["a flow must be a mapping with flow, repos and gates"], []
    unknown = sorted(set(raw) - {"flow", "name", "repos", "gates", "on_failure", "on_push", "version"})
    if unknown:
        errors.append(f"unknown keys {', '.join(unknown)}")
    fid = raw.get("flow")
    if not isinstance(fid, str) or not FLOW_ID_RE.match(fid):
        errors.append("flow: give the flow an id of lowercase letters, digits and dashes (flow: solo-with-agents)")
        fid = "invalid"
    repos = raw.get("repos")
    if isinstance(repos, str):
        repos = [repos]
    if not isinstance(repos, list) or not repos or not all(isinstance(r, str) and _REPO_GLOB_RE.match(r) for r in repos):
        errors.append("repos: list the repositories as owner/repo (wildcards allowed)")
        repos = []
    if visible_repos is not None and repos:
        seen = [v.lower() for v in visible_repos]
        for glob in repos:
            if not any(fnmatch.fnmatchcase(v, glob.lower()) for v in seen):
                errors.append(f"repos: {glob} matches no repository this token can see")
    gates_raw = raw.get("gates")
    if not isinstance(gates_raw, list) or not gates_raw:
        errors.append("gates: list at least one gate (review, check, promote, notify, human)")
        gates_raw = []
    parsed = []
    for i, g in enumerate(gates_raw):
        if isinstance(g, dict) and len(g) == 1 and next(iter(g)) == "review":
            body = g["review"] if isinstance(g["review"], dict) else {}
            if not body.get("by") and default_review_profile:
                g = {"review": {**body, "by": default_review_profile}}
                warnings.append(f"gates[{i}] (review): no by; using the default review profile {default_review_profile}")
        out = _gate(i, g, errors, warnings, flow_id=fid)
        if out is not None:
            parsed.append(out)
    human_at = [i for i, g in enumerate(parsed) if g["type"] == "human"]
    if len(human_at) > 1:
        errors.append("a flow has at most one human gate")
    if human_at and human_at[0] != len(parsed) - 1:
        errors.append("nothing may come after the human gate: the merge is the last step")
    if not human_at:
        warnings.append("no human gate: the desk will not list this flow's pull requests as waiting for you")
    counts: dict = {}
    for g in parsed:
        counts[g["type"]] = counts.get(g["type"], 0) + 1
    seen_n: dict = {}
    gates = []
    for g in parsed:
        seen_n[g["type"]] = seen_n.get(g["type"], 0) + 1
        key = g["type"] if counts[g["type"]] == 1 else f"{g['type']}-{seen_n[g['type']]}"
        gates.append(Gate(type=g["type"], key=key, by=g.get("by") or "", lens=g.get("lens") or "",
                          max_fix_rounds=g.get("max_fix_rounds") or 0, contexts=tuple(g.get("contexts") or ()),
                          timeout_min=g.get("timeout_min") or 0, to=g.get("to") or "", then=g.get("then") or "none",
                          who=g.get("who") or "", action=g.get("action") or "", creators=tuple(g.get("creators") or ()),
                          message=g.get("message") or ""))
    on_failure = raw.get("on_failure", "notify")
    if on_failure not in ON_FAILURE:
        errors.append(f"on_failure must be one of {', '.join(ON_FAILURE)}")
    on_push = raw.get("on_push", "reset_failed")
    if on_push not in ON_PUSH:
        errors.append(f"on_push must be one of {', '.join(ON_PUSH)}")
    version = raw.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        errors.append("version must be a whole number from 1")
        version = 1
    if errors:
        return None, errors, warnings
    name = str(raw.get("name") or fid)[:80]
    return FlowDef(fid, name, tuple(repos), tuple(gates), on_failure, on_push, version, source), [], warnings


# --- YAML out (the shapes are ours, so a small emitter is exact) -------------------------------------

def _scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{},&*!|>'\"%@`]|^\s|\s$|^(true|false|yes|no|null|~|\d)", s, re.I):
        return json.dumps(s, ensure_ascii=False)
    return s


def flow_to_yaml(flow: dict, header: str = "") -> str:
    """Render a flow dict (the file shape: flow, name, repos, gates as one-key maps) as YAML."""
    lines = [f"# {line}" if line else "#" for line in header.splitlines()] if header else []
    for key in ("flow", "name"):
        if flow.get(key):
            lines.append(f"{key}: {_scalar(flow[key])}")
    lines.append("repos: [" + ", ".join(_scalar(r) for r in flow.get("repos") or []) + "]")
    lines.append("gates:")
    for g in flow.get("gates") or []:
        gtype, body = next(iter(g.items()))
        parts = []
        for k, v in (body or {}).items():
            if isinstance(v, (list, tuple)):
                parts.append(f"{k}: [" + ", ".join(_scalar(x) for x in v) + "]")
            else:
                parts.append(f"{k}: {_scalar(v)}")
        lines.append(f"  - {gtype}: {{" + ", ".join(parts) + "}" if parts else f"  - {gtype}: {{}}")
    for key in ("on_failure", "on_push", "version"):
        if key in flow:
            lines.append(f"{key}: {_scalar(flow[key])}")
    return "\n".join(lines) + "\n"
