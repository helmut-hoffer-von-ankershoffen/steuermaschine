"""The two small helpers of the gate's policy in native/lock.py: which push changes only documentation, and the log of
`--allow-full` runs, which never fails the run it records."""

from __future__ import annotations

from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers
from steuermaschine.testing.fakes import FakeCommands
from steuermaschine_heinzel.native import lock


def commands(out: str, rc: int = 0) -> FakeCommands:
    fake = FakeCommands()
    fake.on(["git", "diff", "--name-only"], Result(rc, out, ""))
    return fake


def test_a_push_of_only_documentation_is_recognised_also_behind_env():
    docs = commands("docs/a.md\nREADME.md\n")
    assert lock.docs_only_push(docs, ["git", "push", "origin", "HEAD"])
    assert lock.docs_only_push(docs, ["env", "A=1", "git", "push"])
    assert lock.docs_only_push(docs, ["make", "pr"])


def test_code_an_empty_diff_a_failed_diff_and_other_commands_are_not_documentation_only():
    assert not lock.docs_only_push(commands("src/x.py\ndocs/a.md\n"), ["git", "push"])
    assert not lock.docs_only_push(commands(""), ["git", "push"])
    assert not lock.docs_only_push(commands("docs/a.md\n", rc=1), ["git", "push"])
    assert not lock.docs_only_push(commands("docs/a.md\n"), ["pytest", "tests"])


def test_the_allow_full_log_appends_one_line_and_never_raises(tmp_path):
    clock = fake_providers().clock
    log = tmp_path / "logs" / "allow-full.log"
    lock.log_allow_full(log, ["pytest", "tests"], clock, "/w")
    line = log.read_text().splitlines()[0]
    assert line.endswith("\t/w\tpytest tests") and line.count("\t") == 2
    blocked = tmp_path / "file"
    blocked.write_text("")
    lock.log_allow_full(blocked / "x.log", ["pytest"], clock, "/w")  # its directory cannot exist: the OSError is swallowed
    assert not (blocked / "x.log").exists()
