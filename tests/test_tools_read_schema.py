"""The forgejo_list schema offers every kind the tool handles, and the other way round."""
from fb_fakes import load_plugin, sub

plugin = load_plugin()
read = sub("tools.read")


def test_the_schemas_offer_exactly_the_kinds_the_tools_handle():
    assert plugin.FORGEJO_LIST["parameters"]["properties"]["kind"]["enum"] == list(read.LIST_KINDS)
    assert plugin.FORGEJO_GET["parameters"]["properties"]["kind"]["enum"] == list(read.GET_KINDS)
