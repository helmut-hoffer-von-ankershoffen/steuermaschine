"""One project, three stacks, through the real entry point: core alone, core + project plugin, core + project + layer.

    plugins = ["none"]              the core alone, on purpose
    plugins = ["toy"]               the project plugin (examples/toy)
    plugins = ["toy", "layer"]      the layer on top (examples/layer): LATER ENTRIES ARE OUTER LAYERS

For each stack: `sm --help`, bare `sm` (go), `sm self reviews` and the review gate of the one write command
(`sm sim reset`, which the toy registers with writes=True and which has no approved review).
"""

import json
import subprocess

import pytest

from steuermaschine import INTERNAL, app

STACKS = {"core": '["none"]', "project": '["toy"]', "layered": '["toy", "layer"]'}


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], check=True, capture_output=True)


@pytest.fixture
def project(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "commit", "-q", "--allow-empty", "-m", "init")
    (r / ".git" / "info" / "exclude").write_text("steuermaschine.toml\n.sim/\n")
    monkeypatch.chdir(r)
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    for var in ("STEUERMASCHINE_PLUGIN", "STEUERMASCHINE_PROJECT", "SM_FORMAT", "SM_LAYER__FROZEN", "SM_LAYER__WINDOW_OPEN"):
        monkeypatch.delenv(var, raising=False)

    def use(stack: str):
        (r / "steuermaschine.toml").write_text(f'[project]\nname = "stacked"\nplugins = {STACKS[stack]}\n')
        return r

    return use


def sm(capsys, *args):
    with pytest.raises(SystemExit) as stop:
        app.main(list(args))
    out = capsys.readouterr()
    return stop.value.code, out.out, out.err


def commands_of(help_text: str) -> set[str]:
    """The first words of the command list in `sm --help`."""
    body = help_text.split("Commands:", 1)[1] if "Commands:" in help_text else ""
    return {line.split()[0] for line in body.splitlines() if line.startswith("  ") and not line.startswith("   ")}


@pytest.mark.parametrize("stack", list(STACKS))
def test_help_lists_the_core_and_exactly_what_the_stack_adds(project, capsys, stack):
    project(stack)
    code, out, _ = sm(capsys, "--help")
    words = commands_of(out)
    assert code == 0 and {"self", "go", "head", "ci", "pr"} <= words  # the core's own groups, in every stack
    assert ("sim" in words) is (stack != "core") and ("tools" in words) is (stack != "core")
    code, out, _ = sm(capsys, "--format", "json", "self", "lint")
    assert code == 0 and json.loads(out) == []  # the help contract holds for every stack


@pytest.mark.parametrize("stack", list(STACKS))
def test_bare_sm_is_go_and_reads_only(project, capsys, stack, tmp_path):
    project(stack)
    code, out, err = sm(capsys)
    assert code == 0, err
    assert "PHASE" in out and "NEXT" in out
    same = sm(capsys, "go")
    assert same[0] == 0 and same[1] == out
    assert not (tmp_path / "repo" / ".sim").exists()  # go wrote nothing


def test_the_layers_go_report_hook_runs_only_when_the_layer_is_loaded(project, capsys, monkeypatch):
    monkeypatch.setenv("SM_LAYER__FROZEN", "true")
    project("layered")
    assert "layer: change freeze is ON" in sm(capsys)[1]
    project("project")
    monkeypatch.delenv("SM_LAYER__FROZEN")
    assert "change freeze" not in sm(capsys)[1]


def test_self_reviews_lists_the_projects_write_command_in_every_stack_that_has_it(project, capsys):
    core_units = {"reviews/core-local-writers.md", "reviews/ci-cancel.md", "reviews/lim.md", "reviews/gate-run.md"}  # the core's own writers, in every stack
    project("core")
    code, out, _ = sm(capsys, "--format", "json", "self", "reviews")
    assert code == 0 and {r["file"] for r in json.loads(out)} == core_units
    rows = {}
    for stack in ("project", "layered"):
        project(stack)
        code, out, _ = sm(capsys, "--format", "json", "self", "reviews")
        assert code == 0
        rows[stack] = [(r["path"], r["file"], r["state"], r["digest"], r["files"]) for r in json.loads(out) if r["file"] not in core_units]
    path, file, state, digest, files = rows["project"][0]
    assert (path, file, state) == ("sim reset", "reviews/sim-reset.md", "missing") and "reset.py" in files and len(digest) == 64
    assert rows["layered"] == rows["project"]  # a layer changes neither the review unit nor its digest


def test_the_write_command_is_refused_by_its_review_gate_and_runs_nothing(project, capsys, tmp_path):
    world = tmp_path / "repo" / ".sim" / "world.json"
    project("core")
    code, _, err = sm(capsys, "sim", "reset")
    assert code == 2 and "sim" in err  # the core alone has no such command
    for stack in ("project", "layered"):
        repo = project(stack)
        world.parent.mkdir(exist_ok=True)
        world.write_text('{"ci": {"abc": "green"}, "prs": [], "remote": {}, "tags": {}}\n')
        code, out, err = sm(capsys, "sim", "reset")
        assert code == INTERNAL and "independent review is missing" in err and "reviews/sim-reset.md" in err and "nothing was run" in err, stack
        assert '"abc"' in world.read_text() and out == "", stack  # the world is untouched
        code, out, _ = sm(capsys, "--format", "json", "sim", "reset")
        assert code == INTERNAL and json.loads(out)["code"] == INTERNAL
        assert repo.exists()


def test_the_layer_wraps_engine_steps_but_cannot_open_the_review_gate(project, capsys, monkeypatch):
    """The layer's `sm_step_run` wrapper is the outermost around every pipeline step; a native write command is
    not a pipeline step, so no layer setting runs it without its review."""
    project("layered")
    for name in ("SM_LAYER__FROZEN", "SM_LAYER__WINDOW_OPEN"):
        monkeypatch.setenv(name, "false")
        assert sm(capsys, "sim", "reset")[0] == INTERNAL
