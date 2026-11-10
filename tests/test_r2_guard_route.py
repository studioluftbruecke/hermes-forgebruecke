"""The page's own route that saves a setting: a shell or code call that names it is a settings change like
any other, so it asks first and is refused when nobody is there. The page's click is no tool call."""
import pathlib

import pytest

from fb_fakes import sub

approval = sub("approval")
HOME = pathlib.Path("/srv/no-such-root/profiles/coding")
ROUTE = "http://127.0.0.1:9119/api/plugins/forgebruecke/setting"
CURL = f"curl -s -X POST {ROUTE} -H 'Content-Type: application/json' -d '{{\"key\": \"dry_run\", \"value\": false}}'"
CODE = ("import json, urllib.request\n"
        f"urllib.request.urlopen(urllib.request.Request('{ROUTE}', "
        "data=json.dumps({'key': 'dry_run', 'value': False}).encode(), method='POST'))")


@pytest.mark.parametrize("tool, args", [
    ("terminal", {"command": CURL}),
    ("terminal", {"command": CURL, "workdir": "/srv/some-repo"}),
    ("terminal", {"command": f"http POST {ROUTE.upper()} key=you value=someone"}),
    ("execute_code", {"code": CODE}),
])
def test_a_call_that_names_the_setting_route_asks_first(tool, args):
    assert "setting" in approval.settings_touch(tool, args, HOME)
    asked = approval.make_settings_guard(approval.RunContext, lambda: True, lambda: HOME)(tool_name=tool, args=args)
    assert asked["action"] == "approve" and asked["rule_key"].startswith("forgebruecke:settings:")


def test_nobody_there_means_the_setting_route_is_refused():
    cron = approval.make_settings_guard(lambda: approval.RunContext(cron=True), lambda: True, lambda: HOME)
    refused = cron(tool_name="terminal", args={"command": CURL})
    assert refused["action"] == "block" and "guardrail" in refused["message"]


@pytest.mark.parametrize("command", [
    f"curl -s {ROUTE}s",
    f"curl -s {ROUTE}s?refresh=true",
    "curl -s http://127.0.0.1:9119/api/plugins/forgebruecke/desk",
    "curl -s -X POST http://127.0.0.1:9119/api/plugins/shaderbruecke/setting -d '{}'",
    "grep -rn /api/plugins/forgebruecke/setting plugins/",
])
def test_the_reading_routes_and_another_plugins_route_pass(command):
    assert approval.settings_touch("terminal", {"command": command}, HOME) is None
