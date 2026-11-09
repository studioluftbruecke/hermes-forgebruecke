"""Make the helpers in fb_fakes.py importable by name (pytest runs with --import-mode=importlib), and start
every test with an empty rights cache (access.py caches the agent account's Forgejo rights per process)."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _fresh_rights_cache():
    from fb_fakes import sub
    sub("access").clear_rights()
    yield
    sub("access").clear_rights()
