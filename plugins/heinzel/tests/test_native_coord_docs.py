"""`sm doc section` (the core's docsection) and `sm beads agents add|list`. The agent test's assertions are the former
tool's; the section reader had no test before."""

import json
import subprocess
import sys

from coord_kit import app_runtime

from steuermaschine import docsection
from steuermaschine.commands import doc as docs
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native.agents_lib import table as m

DOC = """# Tracking

intro

## 1. Start here
resume

### Now
| a | b |

```
## not a heading
```

## 6. Work instructions
rule one
rule two

## 8. Log
- 11-12 22:09 queue: batch: 796
- 11-12 22:10 candidate dropped
"""


def test_toc_is_the_default_and_counts_section_sizes(tmp_path):
    (tmp_path / "AGENTS.md").write_text(DOC)
    done = invoke(app_runtime(tmp_path), ["doc", "section"])
    lines = done.stdout.splitlines()
    assert lines[0] == "     1 # Tracking  (22 lines)" and "     5 ## 1. Start here  (10 lines)" in lines and not any("not a heading" in line for line in lines)
    assert [line for line in invoke(app_runtime(tmp_path), ["doc", "section", "--toc", "--level", "1"]).stdout.splitlines()] == ["     1 # Tracking  (22 lines)"]


def test_a_section_by_number_or_text_runs_to_the_next_heading_of_its_level(tmp_path):
    (tmp_path / "AGENTS.md").write_text(DOC)
    done = invoke(app_runtime(tmp_path), ["doc", "section", "--sec", "6", "--sec", "now", "--sec", "nothing"])
    lines = done.stdout.splitlines()
    assert lines[0] == f"=== {tmp_path}/AGENTS.md lines 15-18 ===" and lines[1:4] == ["## 6. Work instructions", "rule one", "rule two"]
    assert f"=== {tmp_path}/AGENTS.md lines 8-14 ===" in lines and "[no heading matches 'nothing']" in lines
    assert docsection.find_sections(DOC.split("\n"), "Tracking") == [(0, 22, "Tracking")]


def test_grep_shows_line_numbers_context_and_the_heading_above_and_cuts(tmp_path):
    (tmp_path / "AGENTS.md").write_text(DOC)
    doc = docs.Section.model_validate(json.loads(invoke(app_runtime(tmp_path), ["--format", "json", "doc", "section", "--grep", "CANDIDATE", "-C", "1"]).stdout))
    assert doc.lines == ["20: - 11-12 22:09 queue: batch: 796", "21: - 11-12 22:10 candidate dropped", "22: ", "   (under: 8. Log)"]
    done = invoke(app_runtime(tmp_path), ["doc", "section", "--grep", "^- ", "-m", "1"])
    assert done.stdout.splitlines()[-1] == "[more matches cut at -m 1]"
    done = invoke(app_runtime(tmp_path), ["doc", "section", "--sec", "1", "--max-lines", "3"])
    assert done.stdout.splitlines()[-1] == "[8 more lines cut; raise --max-lines or narrow with --sec/--grep]"


def test_another_file_relative_to_the_document_and_the_refusals(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(DOC)
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / "log.md").write_text("# Log file\nx\n")
    monkeypatch.chdir(tmp_path / "research")
    assert "# Log file" in invoke(app_runtime(tmp_path), ["doc", "section", "research/log.md"]).stdout
    assert "# Log file" in invoke(app_runtime(tmp_path), ["doc", "section", str(tmp_path / "research" / "log.md")]).stdout
    done = invoke(app_runtime(tmp_path), ["doc", "section", "missing.md"])
    assert done.exit_code == 2 and "cannot read" in done.stderr
    assert invoke(app_runtime(tmp_path), ["doc", "section", "--grep", "("]).exit_code == 2
    assert invoke(app_runtime(tmp_path, coord_document=None), ["doc", "section"]).exit_code == 2


def test_agent_add(tmp_path):
    d = tmp_path / "a.md"
    d.write_text("| Bead |\n| x | PR | a1 | Opus |\n")
    assert "abc" in m.add("5y1", "649", "abc", "Opus", doc=d)
    assert "PR #649" in d.read_text()
    assert m.add("5y1", "649", "abc", "Opus", doc=d).startswith("already")


def test_agents_commands_add_once_list_and_need_the_table(tmp_path):
    (tmp_path / "research").mkdir()
    table = tmp_path / "research" / "agent-ids.md"
    table.write_text("# Agents\n\n| Bead | Work | Agent | Model |\n|---|---|---|---|")
    rt = app_runtime(tmp_path)
    done = invoke(rt, ["beads", "agents", "add", "ligy", "796", "a9b4", "sonnet", "reviewer"])
    assert done.stdout == "| ligy | PR #796 reviewer | a9b4 | sonnet |\n" and table.read_text().endswith("|---|---|---|---|\n| ligy | PR #796 reviewer | a9b4 | sonnet |\n")
    assert invoke(rt, ["beads", "agents", "add", "ligy", "796", "a9b4", "sonnet"]).stdout == "already listed: a9b4\n"
    assert invoke(rt, ["beads", "agents", "add", "x", "-", "b7", "opus"]).stdout == "| x | - | b7 | opus |\n"
    listed = m.AgentRows.model_validate(json.loads(invoke(rt, ["--format", "json", "beads", "agents", "list"]).stdout))
    assert listed.rows[-1] == "| x | - | b7 | opus |" and len(listed.rows) == 3
    other = tmp_path / "other.md"
    assert invoke(app_runtime(tmp_path, agents_table=str(other)), ["beads", "agents", "list"]).exit_code == 2


ADDER = """
import sys
from pathlib import Path
from steuermaschine_heinzel.native.agents_lib import table
doc, worker = Path(sys.argv[1]), sys.argv[2]
for n in range(40):
    table.add(f"b{worker}", str(n), f"a{worker}x{n}", "opus", "builder", doc=doc, lock=Path(sys.argv[3]))
"""


def test_parallel_adds_from_real_processes_keep_every_row(tmp_path):
    """The reviewer's finding: 8 processes x 40 adds left 6 of 532 rows. Real processes, a sandboxed table."""
    doc, lock = tmp_path / "agent-ids.md", tmp_path / "locks" / "agents.lock"
    head = "# Agents\n\n| Bead | PR | Agent | Model |\n|---|---|---|---|\n"
    existing = "".join(f"| old{n} | - | old{n} | opus |\n" for n in range(212))
    doc.write_text(head + existing)
    workers = [subprocess.Popen([sys.executable, "-c", ADDER, str(doc), str(w), str(lock)]) for w in range(8)]
    assert [w.wait(timeout=120) for w in workers] == [0] * 8
    rows = m.rows(doc)
    assert len(rows) == 1 + 212 + 8 * 40 and doc.read_text().startswith(head + existing)
    added = {row.split("|")[3].strip() for row in rows[213:]}
    assert added == {f"a{w}x{n}" for w in range(8) for n in range(40)}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agent-ids.md", "locks"]  # no temporary file left behind


def test_add_refuses_an_unusable_table_and_leaves_it_untouched(tmp_path):
    d = tmp_path / "a.md"
    for data in (b"| Bead | PR | Agent | Model |\n| x | - | \xff\xfe | opus |\n", b"# no table here\n", b"",
                 b"| Bead | PR | Agent | Model |\n| x | - | a1 | opus\n"):
        d.write_bytes(data)
        try:
            m.add("b", "1", "new1", "opus", doc=d)
        except m.TableProblem:
            pass
        else:
            raise AssertionError(data)
        assert d.read_bytes() == data
    d.write_text("| Bead | PR | Agent | Model |\n")
    for bad in ("", "a b", "a|b", "a\nb"):
        try:
            m.add("b", "1", bad, "opus", doc=d)
        except m.TableProblem:
            pass
        else:
            raise AssertionError(bad)
    assert d.read_text() == "| Bead | PR | Agent | Model |\n"


def test_add_matches_whole_cells_and_cleans_the_cells_it_writes(tmp_path):
    d = tmp_path / "a.md"
    d.write_text("| Bead | PR | Agent | Model |\n| ab1 x | PR #1 | a9b4ffff | opus |")
    assert m.add("ab1", "2", "a9b4", "opus", doc=d) == "| ab1 | PR #2 | a9b4 | opus |"  # a9b4 is part of a cell, not a cell
    assert m.add("ab1", "2", "a9b4", "opus", doc=d) == "already listed: a9b4"
    assert m.add("x|y", "3", "c1", "son\nnet", "rev | iewer", doc=d) == "| x/y | PR #3 rev / iewer | c1 | son net |"
    assert d.read_text().endswith("| a9b4ffff | opus |\n| ab1 | PR #2 | a9b4 | opus |\n| x/y | PR #3 rev / iewer | c1 | son net |\n")
    assert (tmp_path / "a.md.lock").exists()


def test_agents_add_command_refuses_an_unusable_table_with_exit_1(tmp_path):
    (tmp_path / "research").mkdir()
    table = tmp_path / "research" / "agent-ids.md"
    table.write_text("not a table\n")
    lock = tmp_path / "elsewhere.lock"
    done = invoke(app_runtime(tmp_path, agents_lock_file=str(lock)), ["beads", "agents", "add", "x", "1", "c1", "opus"])
    assert done.exit_code == 1 and "has no table row" in done.stderr and table.read_text() == "not a table\n" and lock.exists()
