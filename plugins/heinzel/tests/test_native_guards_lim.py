"""`sm lim` (the core's command, with the plugin's `guards_lim_dir`) through the command tree, and the moved tests of the former output limiter (its smoke tests).

Moved: `condense` passes short output and cuts long output keeping the error lines (called as the former test called
it); the main path logs the full output and keeps the exit code (the same `seq 1 100; exit 3` pipeline, run by the real
process provider, through the command; the old test called `main([...])`).
"""

import json

import guards_runtime as g
import pytest

from steuermaschine.commands import lim
from steuermaschine.commands.lim import Limited
from steuermaschine.defaults import Subprocess
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, invoke


def test_lim_passes_short_output_and_cuts_long_keeping_errors():
    short = [f"l{i}" for i in range(10)]
    assert lim.condense(short) == (short, False)
    long = [f"line {i}" for i in range(200)]
    long[100] = "FAILED test_x - boom"
    out, cut = lim.condense(long, head=3, tail=3)
    assert cut and any("FAILED test_x" in x for x in out) and len(out) < 12


def test_lim_main_logs_full_output_and_keeps_exit_code(tmp_path):
    log = tmp_path / "full.log"
    done = invoke(g.runtime_of(tmp_path, commands=Subprocess()), ["lim", "-n", "1", "-t", "1", "-o", str(log), "-s", "seq 1 100; exit 3"])
    assert done.exit_code == 3 and len(log.read_text().splitlines()) == 100
    assert "full output 100 lines" in done.stdout and "hz-" not in done.stdout


def test_lim_json_and_the_default_log_directory(tmp_path):
    conf = {"guards_lim_dir": str(tmp_path / "logs")}
    commands = FakeCommands()
    commands.on(["make"], Result(0, "".join(f"row {i}\n" for i in range(80)), "warning: stderr is appended\n"))
    done = invoke(g.runtime_of(tmp_path, table=conf, commands=commands), ["--format", "json", "lim", "-n", "2", "-t", "2", "--", "make", "all"])
    doc = Limited.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and doc.cut and doc.total_lines == 81 and doc.lines[0] == "row 0" and doc.lines[-1].startswith("warning:")
    (written,) = (tmp_path / "logs").glob("*.log")
    assert doc.log == str(written) and written.read_text().endswith("appended\n")
    assert commands.calls == [["make", "all"]] and conf


def test_lim_short_output_is_unchanged_and_writes_no_log(tmp_path):
    commands = FakeCommands()
    commands.on(["echo"], Result(4, "a\nb\n", ""))
    done = invoke(g.runtime_of(tmp_path, table={"guards_lim_dir": str(tmp_path / "logs")}, commands=commands), ["lim", "--", "echo", "a"])
    assert done.exit_code == 4 and done.stdout == "a\nb\n" and not (tmp_path / "logs").exists()
    empty = FakeCommands()
    empty.on(["true"], Result(0, "", ""))
    assert invoke(g.runtime_of(tmp_path, commands=empty), ["lim", "--", "true"]).stdout == "\n"


def test_lim_help_says_the_log_is_written_only_when_output_was_cut(tmp_path):
    text = " ".join(invoke(g.runtime_of(tmp_path, commands=FakeCommands()), ["lim", "--help"]).stdout.split())
    assert "always kept" not in text and "the full output is kept in a log" in text and "writes no log" in text


def test_lim_needs_a_command(tmp_path):
    done = invoke(g.runtime_of(tmp_path, commands=FakeCommands()), ["lim"])
    assert done.exit_code == 2 and "give -s" in done.stderr


@pytest.mark.parametrize(("options", "expected"), [(["-g", "boom"], "58 matching of 58 hidden lines (first 15)"), (["-g", "boom", "-m", "1"], "(first 1)"),
                                                   (["-w", "5"], "…(+"), (["-g", "nothing here"], "58 lines hidden, none match /nothing here/")])
def test_lim_options_reach_the_condenser(tmp_path, options, expected):
    commands = FakeCommands()
    commands.on(["tool"], Result(0, "".join(f"line {i} boom\n" for i in range(60)), ""))
    done = invoke(g.runtime_of(tmp_path, table={"guards_lim_dir": str(tmp_path / "logs")}, commands=commands), ["lim", "-n", "1", "-t", "1", *options, "--", "tool"])
    assert done.exit_code == 0 and expected in done.stdout
