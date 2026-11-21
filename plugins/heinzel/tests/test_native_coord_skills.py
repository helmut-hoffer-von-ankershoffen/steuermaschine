"""`sm doc skills generate|check|link` and the core's skillgen: role skill references from the command tree. The
assertions of the former skills tests that still apply (front matter, description, line limit, stale file, link) moved
here; the tool-to-skill mapping file is gone, so its tests have no successor."""

import json

from coord_kit import app_runtime

from steuermaschine import PROG, helpdoc, skillgen
from steuermaschine.app import command_tree
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import skills as mod

GOOD = """---
name: coordinate
description: Run the loop. Use when a tick starts or before an agent is messaged.
---
At every tick run bare `sm`, then `sm note "what happened"` and `sm alarm list`.

```
sm work sweep --all --propose-close
sm --format json alarm list
```
"""


def skills_of(tmp_path, text=GOOD, name="coordinate"):
    root = tmp_path / "skills"
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "SKILL.md").write_text(text)
    return root


def run(tmp_path, *args, **over):
    return invoke(app_runtime(tmp_path, skills_dir=str(tmp_path / "skills"), **over), ["doc", "skills", *args])


def test_generate_writes_the_help_of_named_commands_and_the_index_once(tmp_path):
    root = skills_of(tmp_path)
    done = run(tmp_path, "generate")
    assert done.stdout.splitlines() == ["wrote coordinate/references/tools.md", "wrote INDEX.md", "generated: 2 file(s) changed"]
    reference = (root / "coordinate/references/tools.md").read_text()
    assert reference.startswith("# Commands for coordinate (generated from the sm command tree; do not edit)\n")
    assert [line for line in reference.splitlines() if line.startswith("## ")] == ["## sm alarm list", "## sm note", "## sm work sweep"]
    assert "Append one dated line to the log at the end of the tracking document." in reference and "Options: --all" in reference
    index = (root / "INDEX.md").read_text()
    assert "| sm note | writes | coordinate |" in index and "| sm alarm list | read-only | coordinate |" in index and "| sm why | read-only |  |" in index
    assert run(tmp_path, "generate").stdout == "generated: 0 file(s) changed\n"
    assert "hz-" not in reference and "hz-" not in index


def test_check_passes_after_generate_when_every_other_command_is_exempt(tmp_path):
    skills_of(tmp_path)
    tree = helpdoc.tree(command_tree(app_runtime(tmp_path)), PROG)  # every first word: other areas add commands at any time
    exempt = sorted({row["path"].split()[1] for row in tree if len(row["path"].split()) > 1})
    assert run(tmp_path, "check", skills_unlisted_ok=exempt).exit_code == 1  # stale: nothing generated yet
    run(tmp_path, "generate")
    done = run(tmp_path, "check", skills_unlisted_ok=exempt)
    assert done.exit_code == 0 and done.stdout == "OK\n"


def test_check_lists_every_kind_of_drift(tmp_path):
    bad = GOOD.replace("name: coordinate", "name: other").replace("Use when a tick starts or before an agent is messaged.", "x" * 1030)
    bad += "Type `sm work fly 12`, `sm nope`, `sm doc`, and never `hz-queue add` or `hz-builder`.\n" + "line\n" * 150
    root = skills_of(tmp_path, bad)
    (root / "plain").mkdir()
    (root / "plain" / "SKILL.md").write_text("no front matter\n")
    (root / "empty").mkdir()  # no SKILL.md: not a skill
    doc = mod.SkillCheck.model_validate(json.loads(invoke(app_runtime(tmp_path, skills_dir=str(root)), ["--format", "json", "doc", "skills", "check"]).stdout))
    text = "\n".join(doc.problems)
    for want in ("coordinate: frontmatter name 'other' != directory", "coordinate: description missing or over 1024 chars",
                 "coordinate: description must say when to use it", "coordinate: SKILL.md over 150 lines", "coordinate: names `sm work fly`, which is no command",
                 "coordinate: names `sm nope`, which is no command", "coordinate/SKILL.md: names the old tool hz-queue (now: sm queue list)", "plain: no frontmatter",
                 "no skill names `sm why`", "stale generated file: INDEX.md"):
        assert want in text, want
    assert "hz-builder" not in text and "no skill names `sm self lint`" not in text and "no skill names `sm note`" not in text
    assert doc.exit_code == 1 and doc.render()[-1].endswith("problem(s)")


def test_link_creates_repairs_and_never_replaces_a_real_directory(tmp_path):
    root = skills_of(tmp_path)
    skills_of(tmp_path, GOOD.replace("name: coordinate", "name: build"), "build")
    a, b = tmp_path / "agents-a", tmp_path / "agents-b"
    (b / "build").mkdir(parents=True)
    (a).mkdir()
    (a / "coordinate").symlink_to(tmp_path)  # points elsewhere: replaced
    done = run(tmp_path, "link", skills_link_dirs=[str(a), str(b)])
    assert sorted(done.stdout.splitlines()) == sorted([f"linked {a}/build", f"linked {a}/coordinate", f"skip {b}/build: exists and is not a symlink", f"linked {b}/coordinate"])
    assert (a / "coordinate").resolve() == (root / "coordinate").resolve() and not (b / "build").is_symlink()
    again = run(tmp_path, "link", skills_link_dirs=[str(a)])
    assert again.stdout == f"every skill is linked in {a}\n"
    assert run(tmp_path, "link").exit_code == 2  # no target configured


def test_the_skills_directory_defaults_to_the_tracker_repository_and_must_exist(tmp_path):
    done = invoke(app_runtime(tmp_path), ["doc", "skills", "check"])
    assert done.exit_code == 2 and f"{tmp_path}/skills does not exist" in done.stderr
    skills_of(tmp_path)
    assert invoke(app_runtime(tmp_path), ["doc", "skills", "generate"]).exit_code == 0


def test_mentions_are_read_from_code_only_and_resolve_to_the_longest_path():
    text = "Prose about sm queue add is ignored. `sm queue add 12 --bead x`; `cd x && sm go`; bare `sm`; `smx go`; `sm-tool`.\n```\n(sm queue list)\n```\n"
    assert skillgen.mentions(text, "sm") == [["queue", "add"], ["go"], [], ["queue", "list"]]
    rows = {"": {}, "queue": {"group": True}, "queue add": {}, "go": {}}
    assert skillgen.resolve(["queue", "add", "now"], rows, {"queue"}) == ("queue add", "")
    assert skillgen.resolve(["queue", "fly"], rows, {"queue"}) == ("queue", "fly") and skillgen.resolve(["nope"], rows, {"queue"}) == ("", "nope")
    assert skillgen.resolve([], rows, {"queue"}) == ("", "") and skillgen.resolve(["go", "fast"], rows, {"queue"}) == ("go", "")
