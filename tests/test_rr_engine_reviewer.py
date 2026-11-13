""""Create a reviewer profile" says what the copy takes before it runs, needs that confirm on the route too,
and leaves the copy without the user's memories or Forgebrücke's tokens. It never opens a .env itself."""
import types

from fb_fakes import sub

desk = sub("desk")


class CloningHermes:
    """`hermes profile create --clone-from`, `memory reset` and `config unset` as Hermes does them on disk: the clone
    copies config.yaml, .env, SOUL.md and both memory files of the source."""

    def __init__(self, root):
        self.root, self.calls = root, []

    def __call__(self, cmd, **kw):
        args = cmd[1:]
        self.calls.append(args)
        if args[:2] == ["profile", "create"]:
            src, dst = self.root / args[args.index("--clone-from") + 1], self.root / args[2]
            for rel in ("config.yaml", "SOUL.md", "memories/MEMORY.md", "memories/USER.md"):
                if (src / rel).exists():
                    (dst / rel).parent.mkdir(parents=True, exist_ok=True)
                    (dst / rel).write_bytes((src / rel).read_bytes())
        elif args[2:4] == ["memory", "reset"]:
            assert args[:2] == ["-p", desk.REVIEWER] and "--yes" in args and args[-2:] == ["--target", "all"]
            for rel in ("memories/MEMORY.md", "memories/USER.md"):
                (self.root / args[1] / rel).unlink(missing_ok=True)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


def source(tmp_path):
    home = tmp_path / "profiles" / "coding"
    (home / "memories").mkdir(parents=True)
    (home / "config.yaml").write_text("model: x\n")
    (home / "SOUL.md").write_text("me\n")
    (home / "memories" / "MEMORY.md").write_text("staging token rotates Fridays\n")
    (home / "memories" / "USER.md").write_text("lives at 12 Example St\n")
    return home


def test_without_the_confirm_nothing_runs_and_the_answer_names_what_a_copy_takes(tmp_path):
    hermes = CloningHermes(tmp_path / "profiles")
    status, body = desk.api_create_reviewer({}, source="coding", home=source(tmp_path), run=hermes, hermes="/bin/hermes")
    assert status == 409 and body["error"] == "confirm_needed" and hermes.calls == []
    for name in ("config.yaml", ".env", "SOUL.md", "MEMORY.md", "USER.md"):
        assert name in body["message"], name


def test_a_confirmed_copy_has_no_memories(tmp_path):
    hermes = CloningHermes(tmp_path / "profiles")
    status, body = desk.api_create_reviewer({"confirm": True}, source="coding", home=source(tmp_path), run=hermes,
                                            hermes="/bin/hermes")
    assert status == 200 and body["ok"] and body["created"]
    reviewer = tmp_path / "profiles" / desk.REVIEWER
    assert (reviewer / "config.yaml").exists()
    assert not (reviewer / "memories" / "MEMORY.md").exists() and not (reviewer / "memories" / "USER.md").exists()
    assert ["-p", desk.REVIEWER, "memory", "reset", "--yes", "--target", "all"] in hermes.calls
    assert "verdict file" not in str(hermes.calls)


def test_the_page_shows_the_same_sentence_the_route_asks_to_confirm():
    assert desk.reviewer_copy_text("coding") == desk.api_create_reviewer({}, source="coding", hermes="/x")[1]["message"]
