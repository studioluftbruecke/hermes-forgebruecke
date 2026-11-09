"""M10: learn mode's lesson cards. Three lessons, each shown once per user, at the end of an answer when its
trigger fires and the user switched ``learn_mode`` on.

    first_pr            a receipt verified that a pull request was opened
    first_merge         a receipt verified that a pull request was merged
    first_failed_check  the answer reports a failed check or test

Lessons ride on the receipts hook (``transform_llm_output``): Hermes keeps the first non-None rewrite, so a
second hook of the same plugin could never add to the receipts. Shown lessons live in
``<data_dir>/lessons.json`` as ``{user_key: [lesson_id, ...]}``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    body: str
    trigger: str


LESSONS = (
    Lesson("first_pr", "your first pull request",
           "A pull request asks to bring the changes of one branch into another, usually `main`. Nothing reaches "
           "`main` yet: reviews and checks attach to the pull request first, and you decide when it merges. You "
           "can open it in Forgejo, read the changed files, and comment on any line.", "first_pr"),
    Lesson("first_failed_check", "a failed check",
           "A check is a small report on one commit, for example `devops/tests = failure`. Failed means the code "
           "on that commit did not pass; nothing was merged. Fix it on the same branch and push again: the "
           "checks run on the new commit, and the old result stays on the old one.", "first_failed_check"),
    Lesson("first_merge", "your first merge",
           "Merging copies the pull request's commits into the target branch, and the pull request closes. The "
           "branch it came from can be deleted now; its history lives on in `main`. If something is wrong after a "
           "merge, a new pull request fixes it; nothing is lost.", "first_merge"),
)
BY_ID = {lesson.id: lesson for lesson in LESSONS}
_FAILED_CHECK = re.compile(
    r"\b(check|checks|tests?|ci|build|status|gate|review)\b[^.\n]{0,60}\b(failed|failing|failure|is red|went red)\b|"
    r"\b(failed|failing)\b[^.\n]{0,30}\b(check|checks|tests?|ci|build)\b", re.I)


def might_trigger(text: str) -> bool:
    """Cheap pre-check, no I/O: could a lesson fire on this text without any receipt?"""
    return bool(_FAILED_CHECK.search(text or ""))


def triggers(text: str, receipts: list) -> list:
    out = []
    for r in receipts:
        if r.verified is True and r.kind == "pr":
            out.append("first_pr")
        if r.verified is True and r.kind == "merge":
            out.append("first_merge")
    if _FAILED_CHECK.search(text or ""):
        out.append("first_failed_check")
    return list(dict.fromkeys(out))


class LessonStore:
    def __init__(self, data_dir: Any) -> None:
        self.path = Path(data_dir) / "lessons.json" if data_dir else None

    def shown(self) -> dict:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def mark(self, user: str, lesson_id: str) -> None:
        if self.path is None:
            return
        data = self.shown()
        data.setdefault(user, [])
        if lesson_id not in data[user]:
            data[user].append(lesson_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


def card(lesson: Lesson) -> str:
    return f"> **Learn: {lesson.title}.** {lesson.body}"


def pick(text: str, receipts: list, store: LessonStore, user: str) -> Optional[str]:
    """At most one card per answer: the first trigger whose lesson this user has not seen. Marks it shown."""
    seen = set(store.shown().get(user, []))
    for lesson_id in triggers(text, receipts):
        if lesson_id not in seen:
            store.mark(user, lesson_id)
            return card(BY_ID[lesson_id])
    return None


def user_key() -> str:
    try:
        from gateway.session_context import get_session_env
        platform = get_session_env("HERMES_SESSION_PLATFORM", "") or "local"
        user = get_session_env("HERMES_SESSION_USER_ID", "") or "owner"
    except Exception:
        platform, user = "local", "owner"
    return f"{platform}:{user}"
