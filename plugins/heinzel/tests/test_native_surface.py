"""The visible surface of this project (native/surface.py): exactly the twelve verbs, `sm -h` within its contract, every
NEXT a visible command that parses, the cold-help eval table, and the paths service jobs and hooks call still work."""

import inspect
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from coord_kit import app_runtime

from steuermaschine import PROG, helpdoc, surface
from steuermaschine.app import command_tree
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import why_subject
from steuermaschine_heinzel.native.go_lib.words import Words
from steuermaschine_heinzel.native.status_lib import cap
from steuermaschine_heinzel.native.surface import RENAMES, SURFACE

EVALS = Path(__file__).resolve().parents[1] / "evals" / "cold_help.toml"
VERBS = ("check", "ship", "review", "release", "why", "status", "work", "mini", "alarm", "note", "self")
# what launchd jobs, hooks and the release train run as child processes: hidden, never stubs
CALLED = ("daemon queue", "daemon train", "daemon loadguard", "mini leakwatch sample", "guard git", "guard heavy", "guard uv", "gate run", "go",
          "release resume", "head ship", "queue add", "queue list", "ci defer", "ci deferred", "ci rerun", "release train resume-ci", "mini update",
          "release verify", "release train status", "alarm asks")


def rows(tmp_path):
    return helpdoc.tree(command_tree(app_runtime(tmp_path)), PROG)


def test_exactly_the_declared_commands_are_visible(tmp_path):
    shown = {r["path"].removeprefix(PROG).strip() for r in rows(tmp_path) if not r["hidden"]} - {""}
    assert shown == set(SURFACE.visible)
    assert {p for p in shown if " " not in p} == set(VERBS)
    assert not {"head", "queue", "ci", "guard", "daemon", "doc", "beads", "pr", "go", "tick"} & shown


def test_sm_h_is_the_contract(tmp_path):
    out = invoke(app_runtime(tmp_path), ["-h"]).stdout
    assert surface.help_findings(out) == [] and len(out.splitlines()) <= 60
    order = [line.split()[1] for line in out.splitlines() if line.startswith("  sm ")]
    assert order[1:] == list(VERBS)  # the first line is bare `sm`
    assert invoke(app_runtime(tmp_path), ["self", "lint"]).exit_code == 0


def test_jobs_and_hooks_keep_their_paths(tmp_path):
    by_path = {r["path"].removeprefix(PROG).strip(): r for r in rows(tmp_path)}
    for path in CALLED:
        assert path in by_path and not by_path[path]["purpose"].startswith("Renamed:"), path


def sample(method):
    args = []
    for name, param in inspect.signature(method).parameters.items():
        if param.default is not inspect.Parameter.empty:
            continue
        args.append({"version": "0.9.55", "number": 891, "run": 44934734258, "target": 891, "worktree": "/tmp/wt", "bead": "lb7p", "subject": "fix(web): a title",
                     "key": "stall:x", "text": "sm ci why 12", "verb": "admit", "arg": "/tmp/wt", "rest": "", "has_drop": True, "what": "x"}.get(name, "x"))
    return args


SKIP = {"trap", "rename", "alarm_text", "apply_head", "queue_tick", "release_finishing", "focus", "rehearse", "pipeline"}  # not commands, or history


def test_every_next_is_a_visible_command_that_parses(tmp_path):
    root = command_tree(app_runtime(tmp_path))
    words = Words()
    texts = [getattr(words, name)(*sample(getattr(words, name))) for name, _ in inspect.getmembers(words, inspect.ismethod) if name not in SKIP]
    texts += [words.pipeline("head", "admit", "/tmp/wt"), words.pipeline("release", "cut", "0.9.55"), words.drop("0.9.55", False), words.focus("list")]
    texts += [f"{r.old} --help" for r in RENAMES]  # every old name, translated, is a visible command
    problems = []
    for text in texts:
        shown = surface.translate(text if text.startswith("sm") or " sm " in text else f"{PROG} {text}", RENAMES)
        for line in surface.commands_in(shown):
            if (found := surface.dry_run(root, line)):
                problems.append(f"{text!r} -> {shown!r}: {found}")
    assert not problems, problems


def test_the_cold_help_table_names_visible_commands(tmp_path):
    cases = tomllib.loads(EVALS.read_text())["case"]
    assert len(cases) == 20
    root = command_tree(app_runtime(tmp_path))
    visible = {r["path"].removeprefix(PROG).strip() for r in rows(tmp_path) if not r["hidden"]}
    for case in cases:
        assert case["expect"] in visible, case
        line = surface.commands_in(case["example"])[0]
        assert surface.dry_run(root, line) == "", case
        assert " ".join(line).startswith(case["expect"]), case


def test_the_resolver_answers_an_old_name(tmp_path):
    out = invoke(app_runtime(tmp_path), ["sonar-close"])
    assert out.exit_code == 2 and "Did you mean: sm review sonar [PR]" in out.stderr
    out = invoke(app_runtime(tmp_path), ["ci", "sonar-close", "--pull-request", "1"])
    assert out.exit_code == 2 and "Renamed: sm review sonar (was: sm ci sonar-close)" in out.stderr and out.stdout == ""
    out = invoke(app_runtime(tmp_path), ["beads", "tasks", "add", "a title"])
    assert out.exit_code == 2 and "Renamed: sm work add (was: sm beads tasks add)" in out.stderr


def test_why_picks_the_subject_kind():
    assert [why_subject.kind(s) for s in (None, "890", "44934734258", "0.9.54", "v0.9.54", "main", "fix/topic", "lb7p")] == \
        ["none", "pr", "run", "version", "version", "main", "branch", "bead"]


def test_the_document_cap_moves_old_checkpoints_then_log_lines():
    head = "# Doc\n## 5. State\n"
    blocks = "".join(f"### checkpoint {n}\n" + "x" * 50 + "\n" for n in range(6))
    log = "## 8. Log\n" + "".join(f"- 11-21 10:{n:02d} line {n}\n" for n in range(30))
    text = head + blocks + "## 6. Work\n" + log
    small, moved = cap.apply(text, cap.Cap(limit=10**6))
    assert (small, moved) == (text, "")
    capped, moved = cap.apply(text, cap.Cap(limit=400, keep=3, keep_log=5))
    assert "### checkpoint 2" in capped and "### checkpoint 3" not in capped and "### checkpoint 5" in moved
    assert "line 29" in capped and "line 24" not in capped and "line 0" in moved and "## 8. Log" in capped
    assert cap.apply("y" * 500, cap.Cap(limit=10)) == ("y" * 500, "")


def test_the_cap_keeps_the_state_block_that_sits_inside_the_last_checkpoints_range():
    block = "<!-- sm status --write: begin (generated; do not edit) -->\n## State now\n- x\n<!-- sm status --write: end -->\n"
    text = "# Doc\n## 5. State\n" + "".join(f"### c{n}\n" + "x" * 50 + "\n" for n in range(6)) + block + "## 6. Work\nrules\n"
    capped, moved = cap.apply(text, cap.Cap(limit=300, keep=3))
    assert block in capped and "### c2" in capped and "### c3" not in capped and "## 6. Work" in capped
    assert "### c5" in moved and "State now" not in moved and "- x" not in moved


def test_status_write_renders_the_state_section_and_caps_the_document(tmp_path, monkeypatch):
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native import alarm
    from steuermaschine_heinzel.native import status as status_mod
    from steuermaschine_heinzel.native.handoff_lib import hour

    doc = tmp_path / "AGENTS.md"
    doc.write_text("# Doc\n## 5. State\n" + "".join(f"### c{n}\n" + "x" * 200 + "\n" for n in range(5)) + "## 6. Work\nrules\n## 8. Log\n- 11-21 10:00 one\n")
    monkeypatch.setattr(alarm, "alarm_list", lambda check=False: alarm.Alarms(checked=False))
    monkeypatch.setattr(alarm, "owner_asks", lambda: ["one message"])
    monkeypatch.setattr(hour, "build", lambda *a, **k: SimpleNamespace(render=lambda: ["facts"]))
    rt = app_runtime(tmp_path, coord_document=str(doc), status_doc_cap_bytes=1400, coord_log_dir=str(tmp_path / "log"))
    with using(NativeRuntime(rt.providers, rt.settings, rt.project, commands=lambda: [])):
        lines = status_mod.write_document(False, status_mod.Status())
        again = status_mod.write_document(False, status_mod.Status())
    text = doc.read_text()
    assert text.count(status_mod.BEGIN) == 1 and text.index(status_mod.BEGIN) < text.index("## 6. Work") and "- one message" in text
    assert "### c2" in text and "### c3" not in text and lines[-1] == "facts" and again[-1] == "facts"
    archive = next((tmp_path / "log").glob("*.md")).read_text()
    assert "### c4" in archive


def test_place_replaces_or_appends_the_state_section():
    from steuermaschine_heinzel.native import status as status_mod

    block = f"{status_mod.BEGIN}\nnew\n{status_mod.END}"
    assert status_mod.place(f"a\n{status_mod.BEGIN}\nold\n{status_mod.END}\nb", block) == f"a\n{block}\nb"
    assert status_mod.place("a\nb", block) == f"a\nb\n{block}\n"


def test_status_write_is_one_short_checkpoint_and_never_grows_the_document_past_its_cap(tmp_path, monkeypatch):
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native import alarm
    from steuermaschine_heinzel.native import status as status_mod
    from steuermaschine_heinzel.native.handoff_lib import hour

    doc = tmp_path / "AGENTS.md"
    doc.write_text("# Doc\n## 5. State\n## 6. Work\nrules\n## 8. Log\n- 11-21 10:00 one\n")
    monkeypatch.setattr(alarm, "alarm_list", lambda check=False: alarm.Alarms(checked=False))
    monkeypatch.setattr(alarm, "owner_asks", lambda: [f"ask {n} " + "w" * 400 for n in range(3200)])  # the 3200 lines of the incident
    monkeypatch.setattr(hour, "build", lambda *a, **k: SimpleNamespace(render=lambda: ["facts"]))
    rt = app_runtime(tmp_path, coord_document=str(doc), coord_log_dir=str(tmp_path / "log"))
    with using(NativeRuntime(rt.providers, rt.settings, rt.project, commands=lambda: [])):
        status_mod.write_document(False, status_mod.Status())
    text = doc.read_text()
    block = text[text.index(status_mod.BEGIN):text.index(status_mod.END)].splitlines()
    assert len(block) <= status_mod.CHECKPOINT_LINES and len(text.encode()) <= 16 * 1024 and "3197 more" in text
    assert "ask 3199" in next((tmp_path / "log").glob("*.md")).read_text()  # the long facts are in the log


def test_status_write_refuses_an_append_past_the_cap_and_writes_nothing(tmp_path, monkeypatch):
    from steuermaschine.engine import Refuse
    from steuermaschine.native import NativeRuntime, using
    from steuermaschine_heinzel.native import alarm
    from steuermaschine_heinzel.native import status as status_mod

    doc = tmp_path / "AGENTS.md"
    before = "# Doc\n## 5. State\n## 6. Work\n" + "rule line " * 20 + "\n"  # no checkpoints or log to move: nothing can shrink it
    doc.write_text(before)
    monkeypatch.setattr(alarm, "alarm_list", lambda check=False: alarm.Alarms(checked=False))
    monkeypatch.setattr(alarm, "owner_asks", lambda: ["one"])
    rt = app_runtime(tmp_path, coord_document=str(doc), status_doc_cap_bytes=len(before) + 20, coord_log_dir=str(tmp_path / "log"))
    with using(NativeRuntime(rt.providers, rt.settings, rt.project, commands=lambda: [])), pytest.raises(Refuse, match="over the cap"):
        status_mod.write_document(False, status_mod.Status())
    assert doc.read_text() == before
