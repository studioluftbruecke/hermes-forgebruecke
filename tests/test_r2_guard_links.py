"""The settings guard compares paths as text. These cases pin that a link to the profile folder, or to the
folder above it, still leads to the profile's settings file."""
import pytest

from fb_fakes import sub

approval = sub("approval")


@pytest.fixture
def places(tmp_path):
    home = tmp_path / "profiles" / "coding"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text("plugins:\n  enabled: [forgebruecke]\n", encoding="utf-8")
    (tmp_path / "alias").symlink_to(home)
    (tmp_path / "up").symlink_to(home.parent)
    return {"home": home, "alias": tmp_path / "alias", "up": tmp_path / "up"}


@pytest.mark.parametrize("command, workdir", [
    ("sed -i s/a/b/ {alias}/config.yaml", None),
    ("cd {alias} && sed -i s/a/b/ config.yaml", None),
    ("sed -i s/a/b/ config.yaml", "{alias}"),
    ("sed -i s/a/b/ ./config.yaml", "{alias}"),
    ("cd {up} && sed -i s/a/b/ coding/config.yaml", None),
    ("sed -i s/a/b/ coding/config.yaml", "{up}"),
    ("cp x coding/.env", "{up}"),
    ("git -C {alias} checkout -- config.yaml", None),
], ids=["absolute-through-the-link", "cd-into-the-link", "workdir-is-the-link", "workdir-is-the-link-dot-slash",
        "cd-into-a-link-to-the-parent", "workdir-is-a-link-to-the-parent", "workdir-is-a-link-to-the-parent-env",
        "git-C-the-link"])
def test_a_link_to_the_profile_still_leads_to_its_settings_file(places, command, workdir):
    args = {"command": command.format(**places)}
    if workdir:
        args["workdir"] = workdir.format(**places)
    assert approval.settings_touch("terminal", args, places["home"])


def test_a_link_to_another_folder_is_not_the_profile(places, tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "other").symlink_to(tmp_path / "repo")
    assert approval.settings_touch("terminal", {"command": "sed -i s/a/b/ config.yaml",
                                                "workdir": str(tmp_path / "other")}, places["home"]) is None
