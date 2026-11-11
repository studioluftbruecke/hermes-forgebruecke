"""The plugin's version, one constant for the Python half. ``plugin.yaml``, ``dashboard/manifest.json`` and
``desktop/plugin.js`` carry the same string; ``tests/test_settings.py`` holds them equal. The routes return it, so
the Desktop half can say when it is older than Hermes' half, and the mention channel compares it with the code on
disk to notice an update that only half arrived.
"""
VERSION = "0.0.21"
