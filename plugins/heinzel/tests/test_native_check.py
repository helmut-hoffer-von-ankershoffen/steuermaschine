"""`sm check` through the whole command tree on a scripted machine: which checks a change starts, where each command
comes from (the checkout's selector and hook, never a list in sm), and the receipt bound to the commit.
No network, no real git, uv or node; the lock files live under tmp_path."""

import json

from test_native_preflight import HEAD, MAIN, Machine, core, rt  # noqa: F401  (core and rt are fixtures)

from steuermaschine import verdicts
from steuermaschine.protocols import Result
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native import check

# The checkout's own selector: `sm check` runs what THIS says, so a test of another name proves nothing is listed in sm.
SELECTOR = '''
class Repo:
    def __init__(self, root):
        self.root, self.lanes = root, {}
def classify(repo, changed):
    full = "pyproject.toml" in changed
    pytest = (["tests/unit/prose_rules_test.py"] if any(p.endswith(".md") for p in changed) else []) + (["tests/unit/x_test.py"] if any(p.startswith("src/") for p in changed) else [])
    return {"files": sorted(changed), "full": full, "pytest": ["ALL"] if full else pytest,
            "vitest": ["ALL"] if full else [p.replace(".ts", ".test.ts") for p in changed if p.startswith("web/src/")],
            "e2e_lanes": ["web-1"] if any(p.startswith("web/") for p in changed) else [], "jobs": [], "reasons": []}
'''
WEB = {"web": ["web-prepare=uv run --script scripts/web_build.py prepare", "&web-lint=uv run --script scripts/web_build.py exec biome ci .",
               "&web-types=uv run --script scripts/web_build.py exec tsc --noEmit -p .", "&web-tests=uv run --script scripts/web_build.py exec vitest run --maxWorkers=2 src/a.test.ts"],
       "docs": [], "guards": [], "web_files": ["web/src/a.ts"], "doc_files": [], "note": ""}
DOCS = {"web": [], "docs": ["template=uv run --script scripts/sync_template.py --check", "&typos=uv run typos", "tests=uv run pytest tests/unit/documentation_test.py -n 2 -x -q"],
        "guards": [["uv", "run", "pytest", "tests/integration/guides_test.py", "-n", "2", "-x", "-q"]], "web_files": [], "doc_files": ["docs/adr/0001-x.md"], "note": ""}
NONE = {"web": [], "docs": [], "guards": [], "web_files": [], "doc_files": [], "note": ""}


def checkout(core, hook=NONE, **files):  # noqa: F811
    for path, text in {"Makefile": "all:\n", "scripts/affected.py": SELECTOR, "scripts/scoped_checks.py": "", "src/heinzel/x.py": "X = 1\n", "tests/unit/x_test.py": "",
                       "tests/unit/prose_rules_test.py": "", **files}.items():
        (core / path).parent.mkdir(parents=True, exist_ok=True)
        (core / path).write_text(text)
    return Machine(**{"core_scoped_checks": json.dumps(hook)})


def report(done):
    return check.HeadCheck.model_validate(json.loads(done.stdout))


def step(found, name):
    return next(s for s in found.steps if s.name == name)


def test_a_web_change_runs_the_hooks_web_steps_and_the_selected_vitest_file_and_writes_the_receipt(core, rt, tmp_path):  # noqa: F811
    machine = checkout(core, WEB)
    runtime = rt(machine)
    done = invoke(runtime, ["check", str(core)])
    assert done.exit_code == 0, done
    lines = done.stdout.splitlines()
    assert lines[0] == "sm check aaaaaaaaa (feat/thing) vs origin/main bbbbbbbbb"
    assert [line.split(":")[0].split("] ")[1] for line in lines if line.startswith("  [")] == list(check.cheap.STEPS)
    assert "  [PASS] web: 1 changed web file(s): web-prepare, web-lint, web-types, web-tests green" in lines
    assert "  [PASS] types: only documents, web/, desktop/ or plugins/ changed: the hook runs no type check" in lines and "  [PASS] docs: no document(s) changed" in lines
    assert "         warning: e2e lanes the selector names are not run here: web-1" in lines and "READY" in lines
    assert lines[-1] == f"receipt: checks {HEAD} (written), measured from {MAIN[:12]}: an approval of this exact commit on that base can be recorded"
    assert "  [PASS] hook: scripts/scoped_checks.py answered: 1 web file(s), 0 document(s), 0 early guard command(s)" in lines
    # the hook was asked with the changed paths and the Vitest files the checkout's selector named
    (probe, where, _), = machine.ran("core_scoped_checks")
    assert json.loads(probe[-1]) == {"changed": ["web/src/a.ts"], "vitest": ["web/src/a.test.ts"]} and probe[-2] == str(core) and where == str(core)
    # its steps ran as it printed them, through the run lock, in the worktree
    history = [json.loads(line)["cmd"] for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert history[:4] == [step.split("=", 1)[1] for step in WEB["web"]]
    assert all(call[1] == str(core) and call[2]["HZ_GATE_HELD"] for call in machine.ran("web_build.py"))
    assert not machine.ran("ruff") and not machine.ran("mypy") and not machine.ran("coverage run")  # nothing a web change does not need
    # the diff is against the merge-base, and nothing is pushed or read from GitHub
    assert machine.ran("git merge-base origin/main HEAD") and machine.ran(f"git diff --name-only {MAIN} HEAD")
    assert not machine.ran("git push") and not [call for call in machine.calls if call[0][0] == "gh"]
    receipt = runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)
    assert receipt["green"] is True and receipt["head"] == HEAD and receipt["base"] == MAIN and receipt["branch"] == "feat/thing"
    assert [row["name"] for row in receipt["checks"]] == list(check.cheap.STEPS)
    again = invoke(runtime, ["check", str(core)])  # the same commit again: the receipt is written once
    assert again.exit_code == 0 and again.stdout.splitlines()[-1].startswith(f"receipt: checks {HEAD} (this commit already had one)")


def test_a_document_change_runs_the_docs_style_test_the_selector_names_and_the_hooks_document_steps(core, rt):  # noqa: F811
    machine = checkout(core, DOCS).on("git diff --name-only", "docs/adr/0001-x.md")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert found.ready and found.receipt and found.selector == "core" and found.changed == 1
    assert step(found, "docs").detail == "1 changed document(s): template, typos, tests green" and step(found, "web").detail == "no web file(s) changed"
    assert machine.ran("sync_template.py --check") and machine.ran("uv run typos") and machine.ran("tests/unit/documentation_test.py")
    assert machine.ran("tests/integration/guides_test.py")  # the hook's early guard, as the hook prints it
    (selected,) = [call[0] for call in machine.ran("tests/unit/prose_rules_test.py")]  # the selector's test: no name of it exists in sm
    assert selected[:3] == ["uv", "run", "pytest"] and selected[-1] == "tests/unit/prose_rules_test.py"
    assert step(found, "tests").data == {"tests": ["tests/unit/prose_rules_test.py"], "guards": 1, "selector": "core", "unit_suite": False}
    assert not machine.ran("biome") and not machine.ran("coverage run") and not machine.ran("ruff")


def test_a_source_change_runs_ruff_types_and_the_selected_tests_on_it_and_no_coverage(core, rt):  # noqa: F811
    machine = checkout(core).on("git diff --name-only", "src/heinzel/x.py")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert found.ready and found.receipt, found
    assert [call[0][2:] for call in machine.ran("uv run ruff")] == [["ruff", "format", "--check", "--force-exclude", "--", "src/heinzel/x.py"],
                                                                    ["ruff", "check", "--no-fix", "--force-exclude", "--", "src/heinzel/x.py"]]
    # the type checks name the changed files: never the whole project
    assert [call[0][-3:] for call in machine.ran("uv run mypy")] == [["run", "mypy", "src/heinzel/x.py"]]
    assert [call[0][-3:] for call in machine.ran("uv run pyright")] == [["run", "pyright", "src/heinzel/x.py"]]
    assert not machine.ran("coverage") and "coverage" not in [s.name for s in found.steps]
    assert [call for call in machine.ran("uv run pytest") if "tests/unit/x_test.py" in call[0]]
    assert "clean over 1 changed Python file(s)" in step(found, "types").detail and step(found, "tests").detail == "0 early guard command(s), 1 selected test file(s)"


def src_change(core, **rules):  # noqa: F811
    machine = checkout(core).on("git diff --name-only", "src/heinzel/x.py")
    for needle, answer in rules.items():
        machine.on(needle, answer)
    return machine


def test_a_types_timeout_is_skipped_busy_and_the_run_stays_ready(core, rt):  # noqa: F811
    found = report(invoke(rt(src_change(core, **{"uv run pyright": Result(124, "", "")})), ["--format", "json", "check", str(core)]))
    types = step(found, "types")
    assert found.ready and found.receipt and types.status == "SKIP" and types.detail.startswith("skipped: busy (pyright") and "PR CI" in types.detail


def test_a_real_type_error_fails_even_when_the_other_type_check_is_busy(core, rt):  # noqa: F811
    machine = src_change(core, **{"uv run mypy": Result(1, "x.py:1: error: bad\n", ""), "uv run pyright": Result(124, "", "")})
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert not found.ready and [f.step for f in found.failures] == ["types"] and "mypy failed on the changed files (exit 1)" in found.failures[0].message


def test_a_timeout_of_a_gating_step_fails_and_is_never_skipped(core, rt):  # noqa: F811
    found = report(invoke(rt(src_change(core, **{"tests/unit/x_test.py": Result(124, "", "")})), ["--format", "json", "check", str(core)]))
    assert not found.ready and [f.step for f in found.failures] == ["tests"] and "exit 124" in found.failures[0].message and not found.receipt


def test_a_concurrent_check_waits_for_its_turn_and_gives_up_with_wait(tmp_path):
    import fcntl
    import os

    import pytest

    from steuermaschine.engine import Wait
    from steuermaschine_heinzel.native.check_lib import queue

    said, naps, now = [], [], [0.0]

    def nap(seconds):
        naps.append(seconds)
        now[0] += seconds

    with queue.queued(tmp_path):  # free: no wait, and the lock is released afterwards
        pass
    holder = os.open(tmp_path / queue.LOCK_NAME, os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(Wait), queue.queued(tmp_path, said.append, wait=3.0, poll=1.0, sleep=nap, clock=lambda: now[0]):
            raise AssertionError("the queue was held")
        assert said == ["sm check: another check is running on this machine; waiting for my turn"] and naps == [1.0, 1.0, 1.0]
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    with queue.queued(tmp_path, said.append, wait=3.0, sleep=nap):  # released: the next run gets in at once
        pass


def test_the_queue_hands_the_turn_on_when_the_holder_finishes(tmp_path):
    import fcntl
    import os

    from steuermaschine_heinzel.native.check_lib import queue

    holder = os.open(tmp_path / queue.LOCK_NAME, os.O_RDWR | os.O_CREAT)
    fcntl.flock(holder, fcntl.LOCK_EX)

    def release(_seconds):
        fcntl.flock(holder, fcntl.LOCK_UN)

    with queue.queued(tmp_path, wait=5.0, sleep=release):
        pass
    os.close(holder)


def test_every_finding_comes_in_one_pass_and_a_red_run_writes_no_receipt(core, rt):  # noqa: F811
    machine = checkout(core, WEB).on("exec biome ci", Result(1, "a.ts:1 lint/style\n", "")).on("tsc --noEmit", Result(2, "a.ts(1,1): error TS2322\n", ""))
    machine.on("tests/unit/standards_test.py", Result(1, "FAILED pane actions\n", ""))
    runtime = rt(machine)
    done = invoke(runtime, ["check", str(core)])
    assert done.exit_code == 1 and "NOT READY: 3 failure(s)" in done.stdout
    assert "  1. web: web-lint failed (exit 1): `uv run --script scripts/web_build.py exec biome ci .`" in done.stdout and "| a.ts:1 lint/style" in done.stdout
    assert "  2. web: web-types failed (exit 2)" in done.stdout and "| a.ts(1,1): error TS2322" in done.stdout
    assert "  3. standards: standards tests failed (exit 1)" in done.stdout
    assert done.stdout.splitlines()[-1] == "receipt: none: fix every numbered failure in one commit, then run sm check again"
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    assert runtime.providers.store.load(check.SUMMARY_KIND, HEAD)["ready"] is False  # the red report is kept, as state


def test_no_receipt_for_a_dirty_tree_a_skipped_check_or_a_head_that_moved(core, rt):  # noqa: F811
    dirty = rt(checkout(core, WEB).on("git status --porcelain", " M web/src/a.ts\n"))
    done = invoke(dirty, ["check", str(core)])
    assert done.exit_code == 1 and "NOT READY: 1 failure(s)" in done.stdout and "1. tree: working tree is not clean (1 paths, first: M web/src/a.ts)" in done.stdout
    assert dirty.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    skipped = rt(checkout(core, WEB))
    done = invoke(skipped, ["check", str(core), "--skip", "standards"])
    assert done.exit_code == 0 and "READY (SKIPPED by flag: standards)" in done.stdout
    assert done.stdout.splitlines()[-1] == "receipt: none: standards left out by --skip; a receipt needs every check"
    assert skipped.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    states = iter(["", " M web/src/a.ts\n"])  # clean at the start, edited while the checks ran
    edited = rt(checkout(core, WEB).on("git status --porcelain", lambda argv: next(states)))
    done = invoke(edited, ["check", str(core)])
    assert done.exit_code == 1 and "1. bound: the tree changed while the checks ran" in done.stdout and edited.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    heads = iter([HEAD, "c" * 40])
    moved = rt(checkout(core, WEB).on("git rev-parse HEAD", lambda argv: next(heads) if argv[-1] == "HEAD" else "t" * 40))  # the run lock reads HEAD^{tree}
    done = invoke(moved, ["check", str(core)])
    assert done.exit_code == 1 and "bound: HEAD moved while the checks ran (aaaaaaaaa -> ccccccccc)" in done.stdout
    assert moved.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None and moved.providers.store.get_receipt(verdicts.CHECKS, "c" * 40) is None


def test_a_change_nothing_narrows_leaves_the_whole_unit_suite_to_pr_ci_unless_a_full_run_is_asked_for(core, rt, tmp_path):  # noqa: F811
    """The selector says everything and Core's import graph says ALL. A fast check never runs the whole unit suite (PR CI does): it
    says so and stays READY. Only --allow-full (or the hidden --full) runs it, through the heavy lane; where the lock refuses it the run is red."""
    machine = checkout(core).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", "ALL\n")
    runtime = rt(machine)
    found = report(invoke(runtime, ["--format", "json", "check", str(core)]))
    assert found.ready and not found.failures and step(found, "tests").data["unit_suite"] is False
    assert any("runs on PR CI, not here" in w for w in step(found, "tests").data["warnings"]) and not machine.ran("unit and not perf")  # nothing ran locally
    allowed = rt(machine)
    found = report(invoke(allowed, ["--format", "json", "check", str(core), "--allow-full"]))
    assert found.ready and found.receipt and step(found, "tests").data["unit_suite"] is True
    (suite, where, _), = machine.ran("unit and not perf")
    assert suite[:5] == ["uv", "run", "pytest", "-m", "unit and not perf"] and where == str(core)
    failing = rt(checkout(core).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", "ALL\n").on("unit and not perf", Result(1, "1 failed\n", "")))
    found = report(invoke(failing, ["--format", "json", "check", str(core), "--allow-full"]))
    assert not found.ready and "| 1 failed" in found.failures[0].message and failing.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None


def test_a_failing_import_graph_fails_the_run_instead_of_passing_for_everything(core, rt):  # noqa: F811
    """B1 (the reviewer's probe): scripts/related_tests.py fails or prints something odd. That is no "ALL": red, no receipt."""
    for answer, why in ((Result(3, "", "Traceback"), "scripts/related_tests.py failed (exit 3)"), ("something odd\n", "printed something that is no test path")):
        machine = checkout(core).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", answer)
        runtime = rt(machine)
        found = report(invoke(runtime, ["--format", "json", "check", str(core), "--allow-full"]))
        assert not found.ready and [f.step for f in found.failures] == ["tests"] and why in found.failures[0].message, answer
        assert not machine.ran("unit and not perf") and runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None


def test_a_full_selection_the_import_graph_narrows_runs_those_tests_and_the_ones_of_sel(core, rt, tmp_path):  # noqa: F811
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"python": ["tests/unit/x_test.py::test_one"]}))
    machine = checkout(core).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", "tests/unit/x_test.py\n")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core), "--sel", str(sel)]))
    assert found.ready and step(found, "tests").data["tests"] == ["tests/unit/x_test.py"] and not machine.ran("unit and not perf")
    assert "scripts/related_tests.py narrows the unit tests to 1 file(s)" in step(found, "tests").data["warnings"][0]
    machine = checkout(core).on("git diff --name-only", "pyproject.toml\nsrc/heinzel/x.py").on("scripts/related_tests.py", "ALL\n")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))  # src/ changed and no test named: the whole suite is PR CI's job, with a warning
    assert found.ready and "coverage" not in [s.name for s in found.steps] and "the whole unit suite runs on PR CI" in step(found, "tests").data["warnings"][0]


def test_a_hook_that_does_not_answer_fails_the_run_whatever_changed(core, rt, tmp_path):  # noqa: F811
    """B1 (the reviewer's probe): scripts/scoped_checks.py crashes and no web file or document changed. The early
    guards of the change are unknown, so the run is red; and a checkout without the hook gets no receipt either."""
    for changed in ("src/heinzel/x.py", "web/src/a.ts", "docs/adr/0001-x.md"):
        machine = checkout(core).on("core_scoped_checks", json.dumps({"error": "AttributeError: no scoped_steps"})).on("git diff --name-only", changed)
        machine.on("-m coverage run", Result(1, "", ""))
        runtime = rt(machine)
        found = report(invoke(runtime, ["--format", "json", "check", str(core)]))
        assert "hook" in [f.step for f in found.failures] and not found.receipt and runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None, changed
        hook = next(f for f in found.failures if f.step == "hook")
        assert "scripts/scoped_checks.py does not answer: AttributeError" in hook.message and "early guards of this head are unknown" in hook.message
        assert step(found, "web").status == "SKIP" and step(found, "docs").status == "SKIP"
    silent = rt(checkout(core).on("core_scoped_checks", Result(1, "", "uv: error")))
    assert [f.message.split(":")[0] for f in report(invoke(silent, ["--format", "json", "check", str(core)])).failures] == ["scripts/scoped_checks.py could not be asked (exit 1)"]
    machine = checkout(core)
    (core / "scripts/scoped_checks.py").unlink()
    absent = rt(machine)
    found = report(invoke(absent, ["--format", "json", "check", str(core)]))
    assert [f.step for f in found.failures] == ["hook"] and "Core has no scripts/scoped_checks.py" in found.failures[0].message
    assert absent.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None and not machine.ran("core_scoped_checks")


def test_checks_measured_from_another_base_write_no_receipt(core, rt):  # noqa: F811
    """B2 (the reviewer's probe): `--base HEAD~1` checks the last commit only. Green, but no receipt."""
    runtime = rt(checkout(core, WEB))
    done = invoke(runtime, ["check", str(core), "--base", "HEAD~1"])
    assert done.exit_code == 0 and "READY" in done.stdout.splitlines()
    assert done.stdout.splitlines()[-1] == "receipt: none: measured from --base HEAD~1, not from origin/main (the setting preflight_base); run sm check without --base for the receipt"
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    done = invoke(runtime, ["check", str(core), "--base", "origin/main"])  # naming the setting's own base is the default
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)["base"] == MAIN and "(written), measured from bbbbbbbbbbbb" in done.stdout


def test_usage_errors_run_nothing(core, rt, tmp_path):  # noqa: F811
    machine = Machine()
    for argv in ([str(tmp_path / "nowhere")], [str(core), "--skip", "everything"]):
        done = invoke(rt(machine), ["--format", "json", "check", *argv])
        assert done.exit_code == 2 and json.loads(done.stdout)["code"] == 2, argv
    assert machine.calls == []
    done = invoke(rt(checkout(core)), ["check", str(core), "--sel", str(tmp_path / "missing.json")])
    assert done.exit_code == 2 and "unreadable selection" in done.stderr
    (core / "Makefile").unlink()  # no core checkout for the selector: a refusal that names the command to look with
    done = invoke(rt(Machine()), ["check", str(core)])
    assert done.exit_code == 2 and "the selector of" in done.stderr and "sm head select --repo" in done.stderr
    no_base = invoke(rt(Machine(**{"git merge-base": Result(1, "", "fatal: no merge base")})), ["check", str(core), "--base", "origin/nope"])
    assert no_base.exit_code == 2 and "has no merge-base with origin/nope" in no_base.stderr
