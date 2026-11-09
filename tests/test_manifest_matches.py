"""The plugin loads, and registers exactly the tools plugin.yaml declares (catalog rule 6).

`hermes plugins validate` fails a registered tool that plugin.yaml does not declare, but only warns
about a declared tool that is never registered. This test fails both.

Run from the plugin folder: python3 -m pytest tests -q --import-mode=importlib
This file is identical in every plugin folder; it stays valid as tools are added.
"""

import importlib.util
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def declared_tools(manifest: str) -> list[str]:
    block = re.search(r"(?m)^provides_tools:[ \t]*\n((?:[ \t]+-[ \t]*[^\s#]+.*\n?)+)", manifest)
    return re.findall(r"(?m)^[ \t]+-[ \t]*([^\s#]+)", block.group(1)) if block else []


def load_plugin():
    name = "plugin_" + ROOT.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class RecordingCtx:
    def __init__(self):
        self.tools = []

    def register_tool(self, **kwargs):
        self.tools.append(kwargs["name"])

    def __getattr__(self, attr):  # any other register_* call is accepted and ignored
        return lambda *a, **k: None


def test_manifest_parser_reads_every_entry():
    manifest = "name: x\nprovides_tools:\n  - a  # note\n  - b\nrequires_env:\n  - X_TOKEN\n"
    assert declared_tools(manifest) == ["a", "b"]


def test_registers_exactly_the_declared_tools():
    ctx = RecordingCtx()
    load_plugin().register(ctx)
    manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert sorted(ctx.tools) == sorted(declared_tools(manifest))
