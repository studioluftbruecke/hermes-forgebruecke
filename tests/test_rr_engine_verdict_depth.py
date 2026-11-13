"""A review answer that nests too deeply, or that the JSON decoder cannot read for any reason, ends the gate in
``error``: reading it never raises, so the gate never stays ``running`` holding a review slot. Ordinary
verdicts read as before."""
import json
import subprocess
from pathlib import Path

import pytest

from fb_fakes import DEMO, HEAD_SHA, sub
from fb_fakes_engine import Kit

runner = sub("flows.runner")
PY313 = Path("/usr/bin/python3.13")


def write(tmp_path, text: str) -> Path:
    path = tmp_path / "answer.json"
    path.write_text(text, encoding="utf-8")
    return path


def verdict(**extra) -> dict:
    return {"verdict": "pass", "head_sha": HEAD_SHA, "summary": "fine", "findings": [], **extra}


class RaisingJson:
    """The ``json`` module with a ``loads`` that raises what CPython 3.11 to 3.13 raise on deep nesting."""

    def __getattr__(self, name):
        return getattr(json, name)

    @staticmethod
    def loads(*_a, **_k):
        raise RecursionError("maximum recursion depth exceeded while decoding a JSON array")


@pytest.mark.parametrize("text", [
    "[" * 20000 + "]" * 20000,
    json.dumps(verdict())[:-1] + ', "x": ' + "[" * 100 + "]" * 100 + "}",
    '{"verdict": "pass", "summary": "' + "{" * 5000 + '", "x": ' + "{\"a\": " * 60 + "1" + "}" * 60 + "}",
])
def test_a_deeply_nested_answer_is_an_error_not_a_verdict(tmp_path, text):
    state, _summary, _findings, problem = runner.read_verdict(write(tmp_path, text), HEAD_SHA)
    assert state == "error"
    assert "nested" in problem


def test_brackets_inside_strings_do_not_count_as_nesting(tmp_path):
    text = json.dumps(verdict(summary="[[[[{{{{" * 20, findings=[{"file": "a.py", "line": 3, "problem": "]]}}" * 30}]))
    assert runner.read_verdict(write(tmp_path, text), HEAD_SHA)[0] == "passed"


def test_any_decoder_exception_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "json", RaisingJson())
    state, _s, _f, problem = runner.read_verdict(write(tmp_path, json.dumps(verdict())), HEAD_SHA)
    assert state == "error" and problem


@pytest.mark.skipif(not PY313.exists(), reason="needs a Python 3.13, whose decoder raises on deep nesting")
def test_python_3_13_reads_a_deeply_nested_answer_as_an_error(tmp_path):
    path = write(tmp_path, "[" * 20000 + "]" * 20000)
    code = ("import importlib.util, sys, pathlib\n"
            "spec = importlib.util.spec_from_file_location('r', sys.argv[1])\n"
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
            "print(m.read_verdict(pathlib.Path(sys.argv[2]), sys.argv[3])[0])\n")
    out = subprocess.run([str(PY313), "-c", code, str(Path(runner.__file__)), str(path), HEAD_SHA],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "error", out.stderr[-400:]


@pytest.mark.parametrize("text, state", [
    (json.dumps(verdict()), "passed"),
    ("```json\n" + json.dumps(verdict(verdict="fail", findings=[{"file": "a.py", "line": 2, "problem": "x"}]))
     + "\n```", "failed"),
    (json.dumps(verdict(findings=[{"file": "a.py", "line": 1, "problem": "p", "notes": {"a": [1, {"b": 2}]}}])),
     "passed"),
])
def test_ordinary_verdicts_read_as_before(tmp_path, text, state):
    assert runner.read_verdict(write(tmp_path, text), HEAD_SHA)[0] == state


def test_the_gate_ends_in_error_when_the_decoder_raises(tmp_path, monkeypatch):
    k = Kit(tmp_path)
    k.tick()
    path = k.store.verdict_path("demo", DEMO, 3, HEAD_SHA, "review")
    path.write_text(json.dumps(verdict()), encoding="utf-8")
    monkeypatch.setattr(runner, "json", RaisingJson())
    report = k.tick()
    assert k.gates()["review"]["state"] == "error"
    assert not report["errors"]
    assert len(k.notifies()) == 1
