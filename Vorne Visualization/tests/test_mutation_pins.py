"""The mutation runner's staleness guard, inside the normal suite.

A mutation whose target text no longer applies tests nothing, and the runner
used to report that as one line among sixty. It has happened four times: Windows
line endings, a stale .pyc, #42's line moving in a refactor, and #41 and #42
again when the product filter was added. Every entry is pinned to a hash of its
target line(s); this fails `pytest` the moment any pin stops matching.
"""

import importlib.util
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("mutation_check", Path(__file__).with_name("mutation_check.py"))
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


@pytest.mark.skipif(os.environ.get("MUTATION_CHECK_RUNNING") == "1",
                    reason="a file is mutated on purpose right now; its pin cannot match")
def test_every_mutation_is_pinned_and_applies():
    assert M.verify() == []


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """A one-file project with one pinned mutation."""
    (tmp_path / "m.py").write_bytes(b"def f(x):\r\n    return x + 1\r\n")    # Windows line endings
    monkeypatch.setattr(M, "ROOT", tmp_path)
    monkeypatch.setattr(M, "MUTATIONS", [("off by one", "m.py", "return x + 1", "return x + 2")])
    monkeypatch.setattr(M, "PINS", {"off by one": M.pin_of("m.py", "return x + 1")})
    return tmp_path


def test_a_pinned_mutation_passes(fake):
    assert M.verify() == []
    assert M.main(["--check"]) == 0


def test_the_pin_ignores_line_endings(fake):
    crlf = M.pin_of("m.py", "return x + 1")
    (fake / "m.py").write_bytes(b"def f(x):\n    return x + 1\n")
    assert M.pin_of("m.py", "return x + 1") == crlf


def test_a_changed_line_refuses_the_run(fake, capsys):
    # the original text still matches as a substring; only the line around it changed
    (fake / "m.py").write_bytes(b"def f(x):\r\n    return x + 1 if x else 0\r\n")
    assert any("changed since it was pinned" in p for p in M.verify())
    assert M.main([]) == 2                                     # nothing is mutated or run
    assert "refusing to run" in capsys.readouterr().out
    assert (fake / "m.py").read_bytes() == b"def f(x):\r\n    return x + 1 if x else 0\r\n"


def test_text_gone_or_doubled(fake):
    (fake / "m.py").write_bytes(b"def f(x):\n    return x - 1\n")
    assert any("found 0 times" in p for p in M.verify())
    (fake / "m.py").write_bytes(b"def f(x):\n    return x + 1\ndef g(x):\n    return x + 1\n")
    assert any("found 2 times" in p for p in M.verify())


def test_an_unpinned_entry_and_an_orphan_pin(fake, monkeypatch):
    monkeypatch.setattr(M, "PINS", {"something else": "0" * 12})
    problems = M.verify()
    assert any("no pin" in p for p in problems) and any("has no mutation" in p for p in problems)


def test_a_mutant_that_cannot_load_is_refused(fake, monkeypatch):
    monkeypatch.setattr(M, "MUTATIONS", [("off by one", "m.py", "return x + 1", "return x + (")])
    assert any("does not load" in p for p in M.verify())


# ------------------------------------- one run at a time, nothing edited mid-run

@pytest.fixture
def project(tmp_path, monkeypatch):
    """A fake project: webapp/m.py with one pinned mutation, and a stand-in for
    the suite that "catches" everything and can do something mid-run."""
    (tmp_path / "webapp").mkdir()
    (tmp_path / "webapp" / "m.py").write_bytes(b"def f(x):\n    return x + 1\n")
    (tmp_path / "webapp" / "other.py").write_bytes(b"Y = 1\n")
    monkeypatch.setattr(M, "ROOT", tmp_path)
    monkeypatch.setattr(M, "LOCK", tmp_path / ".mutation_run.lock")
    monkeypatch.setattr(M, "BACKUP", tmp_path / ".mutation_run.original")
    monkeypatch.setattr(M, "MUTATIONS", [("off by one", "webapp/m.py", "return x + 1", "return x + 2")])
    monkeypatch.setattr(M, "PINS", {"off by one": M.pin_of("webapp/m.py", "return x + 1")})
    calls = {"n": 0, "during": None}

    def suite():
        calls["n"] += 1
        if calls["n"] == 2 and calls["during"]:      # the 1st call is the clean baseline
            calls["during"]()
        return (0, 1) if calls["n"] == 1 else (1, 0)
    monkeypatch.setattr(M, "run_suite", suite)
    return tmp_path, calls


def test_a_clean_run_leaves_nothing_behind(project):
    root, _ = project
    assert M.main([]) == 0
    assert (root / "webapp" / "m.py").read_bytes() == b"def f(x):\n    return x + 1\n"
    assert not (root / ".mutation_run.lock").exists() and not (root / ".mutation_run.original").exists()


def test_a_second_run_refuses_to_start(project, capsys):
    root, calls = project
    (root / ".mutation_run.lock").write_text("pid 1234, started 2026-10-07 08:00:00\n#1 applied to webapp/m.py\n")
    assert M.main([]) == 2
    out = capsys.readouterr().out
    assert "refusing to run" in out and "#1 applied to webapp/m.py" in out
    assert calls["n"] == 0                                    # not even the baseline ran
    assert (root / ".mutation_run.lock").exists()             # not ours to remove


def test_editing_the_mutated_file_mid_run_stops_and_keeps_the_edit(project, capsys):
    root, calls = project
    m = root / "webapp" / "m.py"
    calls["during"] = lambda: m.write_bytes(m.read_bytes() + b"# my edit\n")
    assert M.main([]) == 3
    assert m.read_bytes() == b"def f(x):\n    return x + 1\n"                     # original back
    kept = (root / "webapp" / "m.py.edited-during-mutation").read_bytes()
    assert b"# my edit" in kept and b"x + 2" in kept                              # edit kept, mutation shown
    assert "No verdict" in capsys.readouterr().out
    assert not (root / ".mutation_run.lock").exists()


def test_editing_any_other_source_mid_run_stops(project, capsys):
    root, calls = project
    calls["during"] = lambda: (root / "webapp" / "other.py").write_bytes(b"Y = 2\n")
    assert M.main([]) == 3
    assert "webapp" in capsys.readouterr().out and (root / "webapp" / "other.py").read_bytes() == b"Y = 2\n"
