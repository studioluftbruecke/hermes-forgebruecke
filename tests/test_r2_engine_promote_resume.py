"""A tick that died between saving the promotion pull request and the merge call. The next tick goes on at
the merge, and reads the target again first."""
import pytest

from fb_fakes_engine import PROMOTE, Kit, Stopped, engine_env  # noqa: F401


def die_before_the_merge(k: Kit) -> int:
    save = k.store.save_pr

    def save_then_die(state):
        save(state)
        raise Stopped()

    k.store.save_pr = save_then_die
    with pytest.raises(Stopped):
        k.tick()
    k.store.save_pr = save
    return k.gates()["promote"]["evidence"]["promote_pr"]


def kit(tmp_path) -> Kit:
    k = Kit(tmp_path, flow=PROMOTE)
    k.staging()
    k.keep_opened_pulls()
    return k


def retarget(k: Kit, promo: int) -> None:
    k.data["pulls"][promo]["base"]["ref"] = "main"


def protect(k: Kit, promo: int) -> None:
    k.data["branches"]["staging"]["protected"] = True


def make_default(k: Kit, promo: int) -> None:
    k.data["info"]["default_branch"] = "staging"


@pytest.mark.parametrize("change,detail", [
    (retarget, "pull request #{promo} no longer targets staging, so it was not merged"),
    (protect, "staging is protected; a flow only merges into unprotected branches"),
    (make_default, "staging is the default branch; a flow never merges there"),
], ids=["retargeted", "protected", "default"])
def test_a_resumed_promotion_does_not_merge_into_a_target_that_changed(tmp_path, change, detail):
    k = kit(tmp_path)
    promo = die_before_the_merge(k)
    change(k, promo)
    for _ in range(2):
        k.tick()
    promote = k.gates()["promote"]
    assert k.sent(f"/pulls/{promo}/merge", "POST") == []
    assert promote["state"] == "failed" and promote["detail"] == detail.format(promo=promo)
    assert [b["description"] for b in k.status_posts("failure")] == [promote["detail"]]


def test_a_resumed_promotion_merges_once_when_nothing_changed(tmp_path):
    k = kit(tmp_path)
    promo = die_before_the_merge(k)
    for _ in range(2):
        k.tick()
    assert [path for _, path, _ in k.sent("/merge", "POST")] == [f"/repos/acme/forgebruecke-demo/pulls/{promo}/merge"]
    assert k.gates()["promote"]["state"] == "passed"
