"""`sm check` through the whole command tree on a scripted machine: which checks a change starts, where each command
comes from (the checkout's selector and hook, never a list in sm), and the receipt bound to the commit.
No network, no real git, uv or node; the lock files live under tmp_path."""

import json
from pathlib import Path
from shlex import split

import pytest
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
    assert lines[-1].startswith(f"receipt: checks {HEAD} (written), measured from {MAIN[:12]}: an approval of this exact commit on that base can be recorded. Steps run: tree, ruff,")
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
    assert receipt["requires_full_pr"] is False
    assert [row["name"] for row in receipt["checks"]] == list(check.cheap.STEPS)
    again = invoke(runtime, ["check", str(core)])  # the same commit again: the receipt is written once
    assert again.exit_code == 0 and again.stdout.splitlines()[-1].startswith(f"receipt: checks {HEAD} (this commit already had one)")


def test_deferred_whole_vitest_is_bound_to_check_receipt(core, rt, tmp_path):  # noqa: F811
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"vitest": ["web/src/a.test.ts"]}))
    hook = {**WEB, "web": ["web-tests=web exec vitest run ./src/a.test.ts"]}
    machine = checkout(core, hook).on("git diff --name-only", "pyproject.toml")
    machine.on("web exec vitest run", vitest_report(core, ["web/src/a.test.ts"]))
    runtime = rt(machine)
    result = invoke(runtime, ["--format", "json", "check", str(core), "--sel", str(sel)])
    found = report(result)
    assert found.ready and "the whole Vitest suite" in found.ci_must_run
    receipt = runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)
    assert receipt["head"] == HEAD and receipt["base"] == MAIN and receipt["requires_full_pr"] is True


def vitest_report(worktree, names):
    def write(argv):
        assert "--reporter=default" in argv and "--reporter=json" in argv
        report = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--outputFile.json=")))
        assert not report.exists() and not report.is_relative_to(worktree)
        results = [{"name": str(worktree / name), "status": "passed", "assertionResults": [{"status": "passed"}]} for name in names]
        report.write_text(json.dumps({"success": True, "testResults": results}), encoding="utf-8")
        return "Vitest passed"

    return write


@pytest.mark.parametrize("changed", ["pyproject.toml", "web/src/a.ts"])
def test_named_vitest_green_exit_cannot_hide_a_dropped_suite(core, rt, tmp_path, changed):  # noqa: F811
    wanted = ["web/src/a.test.ts", "web/src/b.test.ts"]
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"vitest": wanted}))
    command = "web exec vitest run ./src/a.test.ts ./src/b.test.ts"
    machine = checkout(core, {**WEB, "web": [f"web-tests={command}"]}).on("git diff --name-only", changed)
    machine.on(command, vitest_report(core, wanted[:1]))
    runtime = rt(machine)
    found = report(invoke(runtime, ["--format", "json", "check", str(core), "--sel", str(sel)]))
    assert not found.ready and not found.receipt
    assert [failure.step for failure in found.failures] == ["web"]
    assert "omits a selection" in found.failures[0].message
    assert not runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)


@pytest.mark.parametrize("automatic, deferred", [("web/src/a.ts\nweb/src/b.ts", True), ("web/src/a.ts", False)])
def test_exact_vitest_runs_only_named_files_and_records_full_pr_when_it_omits_tests(core, rt, tmp_path, automatic, deferred):  # noqa: F811
    wanted = "web/src/a.test.ts"
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"vitest": [wanted]}))
    command = "web exec vitest run ./src/a.test.ts"
    hook = {**WEB, "web": [*WEB["web"][:-1], f"web-tests={command}"]}
    machine = checkout(core, hook).on("git diff --name-only", automatic).on(command, vitest_report(core, [wanted]))
    runtime = rt(machine)
    found = report(invoke(runtime, ["--format", "json", "check", str(core), "--sel", str(sel), "--vitest-exact"]))
    assert found.ready and found.receipt
    (probe, _, _), = machine.ran("core_scoped_checks")
    assert json.loads(probe[-1])["vitest"] == [wanted]
    assert machine.ran(command) and machine.ran("biome ci") and machine.ran("tsc --noEmit")
    receipt = runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)
    assert receipt["head"] == HEAD and receipt["base"] == MAIN and receipt["requires_full_pr"] is deferred
    assert found.ci_must_run == receipt["ci_must_run"] == (["the whole Vitest suite"] if deferred else [])


@pytest.mark.parametrize("reported", [["web/src/a.test.ts", "web/src/nested/src/a.test.ts"], ["web/src/nested/src/a.test.ts"], []])
def test_exact_vitest_green_exit_cannot_hide_extra_or_missing_results(core, rt, tmp_path, reported):  # noqa: F811
    wanted = "web/src/a.test.ts"
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"vitest": [wanted]}))
    command = "web exec vitest run ./src/a.test.ts"
    machine = checkout(core, {**WEB, "web": [f"web-tests={command}"]}).on(command, vitest_report(core, reported))
    runtime = rt(machine)
    found = report(invoke(runtime, ["--format", "json", "check", str(core), "--sel", str(sel), "--vitest-exact"]))
    assert not found.ready and not found.receipt
    assert [failure.step for failure in found.failures] == ["web"]
    assert "reported Vitest" in found.failures[0].message
    assert not runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)


@pytest.mark.parametrize("selection", [None, {}, {"vitest": []}, {"vitest": ["web/src/a b.test.ts"]}])
def test_exact_vitest_refuses_missing_empty_or_invalid_selection(core, rt, tmp_path, selection):  # noqa: F811
    machine = checkout(core)
    runtime = rt(machine)
    args = ["check", str(core), "--vitest-exact"]
    if selection is not None:
        sel = tmp_path / "sel.json"
        sel.write_text(json.dumps(selection))
        args.extend(["--sel", str(sel)])
    result = invoke(runtime, args)
    assert result.exit_code == 2, result
    assert not machine.ran("core_scoped_checks") and not runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)


def test_a_document_change_runs_the_docs_style_test_the_selector_names_and_the_hooks_document_steps(core, rt):  # noqa: F811
    machine = checkout(core, DOCS).on("git diff --name-only", "docs/adr/0001-x.md")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert found.ready and found.receipt and found.selector == "core" and found.changed == 1
    assert step(found, "docs").detail == "1 changed document(s): template, typos, tests green" and step(found, "web").detail == "no web file(s) changed"
    assert machine.ran("sync_template.py --check") and machine.ran("uv run typos") and machine.ran("tests/unit/documentation_test.py")
    pytest_roots = {call[2]["PYTEST_DEBUG_TEMPROOT"] for call in machine.ran("uv run pytest")}
    assert len(pytest_roots) == 1
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


@pytest.mark.parametrize("rc", [75, 124])
def test_a_types_step_with_no_slot_or_a_timeout_fails_and_writes_no_receipt(core, rt, rc):  # noqa: F811
    found = report(invoke(rt(src_change(core, **{"uv run pyright": Result(rc, "", "")})), ["--format", "json", "check", str(core)]))
    assert not found.ready and not found.receipt and [f.step for f in found.failures] == ["types"]
    assert "pyright" in found.failures[0].message and f"(exit {rc})" in found.failures[0].message and step(found, "types").status != "SKIP"


def test_the_receipt_names_the_steps_that_ran_and_says_coverage_and_types_are_ci_enforced(core, rt):  # noqa: F811
    found = report(invoke(rt(src_change(core)), ["--format", "json", "check", str(core)]))
    assert found.ready and found.receipt and "Steps run: " in found.receipt_note and "types" in found.receipt_note and "PR CI enforces" in found.receipt_note


def test_a_real_type_error_and_a_busy_type_check_both_fail(core, rt):  # noqa: F811
    machine = src_change(core, **{"uv run mypy": Result(1, "x.py:1: error: bad\n", ""), "uv run pyright": Result(124, "", "")})
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert not found.ready and [f.step for f in found.failures] == ["types", "types"]
    assert "mypy failed on the changed files (exit 1)" in found.failures[0].message and "pyright ran out of time" in found.failures[1].message


def test_a_timeout_of_a_gating_step_fails_and_is_never_skipped(core, rt):  # noqa: F811
    found = report(invoke(rt(src_change(core, **{"tests/unit/x_test.py": Result(124, "", "")})), ["--format", "json", "check", str(core)]))
    assert not found.ready and [f.step for f in found.failures] == ["tests"] and "exit 124" in found.failures[0].message and not found.receipt


def hold(directory, kind, count):
    """Hold `count` slot files of the pool the way other runs do: an exclusive flock each."""
    import fcntl
    import os

    fds = []
    for n in range(count):
        fd = os.open(directory / f"sm-{kind}.slot.{n}", os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        fds.append(fd)
    return fds


def release(fds):
    import fcntl
    import os

    for fd in fds:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_the_pool_gives_four_check_slots_and_defers_the_fifth_after_60_seconds_without_failing(tmp_path):
    from steuermaschine_heinzel.native.check_lib import slots

    said, now = [], [0.0]

    def nap(seconds):
        now[0] += seconds

    demoted = []
    with slots.pool(tmp_path, demote=lambda: demoted.append(1)) as a, slots.pool(tmp_path) as b, slots.pool(tmp_path) as c, slots.pool(tmp_path) as d:
        assert sorted(g.slot for g in (a, b, c, d)) == [0, 1, 2, 3] and all(g.granted for g in (a, b, c, d)) and demoted == [1]
        with slots.pool(tmp_path, say=said.append, sleep=nap, clock=lambda: now[0]) as fifth:  # never raises, never waits longer than a minute
            assert not fifth.granted and now[0] == 60.0 and "no check slot of 4 was free within 60 s" in fifth.deferred
        assert len(said) == 1 and "waiting up to 60 s" in said[0]
    with slots.pool(tmp_path) as again:  # released: the next run gets a slot at once
        assert again.granted


def test_a_slot_freed_while_waiting_goes_to_the_waiting_run(tmp_path):
    from steuermaschine_heinzel.native.check_lib import slots

    fds = hold(tmp_path, "check", 4)
    with slots.pool(tmp_path, sleep=lambda _s: release(fds[:1]) if fds else None, demote=lambda: None) as grant:
        assert grant.granted and grant.slot == 0


def test_a_machine_over_load_30_is_refused_at_once_and_a_check_starts_at_30(tmp_path):
    from steuermaschine_heinzel.native.check_lib import slots

    naps = []
    with slots.pool(tmp_path, load=lambda: 31.0, sleep=naps.append) as busy:
        assert not busy.granted and "load 31 is over 30" in busy.deferred and not naps  # no waiting for a load
    with slots.pool(tmp_path, load=lambda: 30.0, demote=lambda: None) as edge:
        assert edge.granted  # a check starts at 30, not over it
    assert not hasattr(slots, "SIM_SLOTS")  # no simulator slot: sm check starts no simulator


def test_the_priority_is_lowered_to_background_on_a_grant_only(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native.check_lib import slots

    class Commands:
        calls: list = []

        def run(self, argv, cwd=None, timeout=None, env=None):
            self.calls.append(argv)
            return Result(0, "", "")

    commands = Commands()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(slots.sys, "platform", "darwin")
    with slots.pool(tmp_path, demote=lambda: slots.lower_priority(commands)):
        pass
    assert commands.calls and commands.calls[0][:3] == ["taskpolicy", "-b", "-p"]
    commands.calls.clear()
    with slots.pool(tmp_path, load=lambda: 99.0, demote=lambda: slots.lower_priority(commands)):
        pass
    assert not commands.calls  # a deferred run is never demoted: it runs nothing


def test_a_check_with_all_four_slots_held_exits_75_runs_nothing_and_is_never_ready(core, rt, tmp_path):  # noqa: F811
    fds = hold(tmp_path / "locks", "check", 4)
    try:
        machine = src_change(core)
        runtime = rt(machine)
        done = invoke(runtime, ["check", str(core)])
    finally:
        release(fds)
    assert done.exit_code == 75, done
    assert "no check slot of 4 was free within 60 s" in done.stderr and "READY" not in done.stdout
    assert not machine.ran("ruff") and not machine.ran("mypy") and not machine.ran("pytest") and not machine.ran("scoped_checks")  # nothing ran
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None


def test_a_check_at_load_40_exits_75_and_never_waits(core, rt, monkeypatch):  # noqa: F811
    monkeypatch.setattr("steuermaschine_heinzel.native.check_lib.slots.os.getloadavg", lambda: (40.0, 30.0, 20.0))
    machine = src_change(core)
    runtime = rt(machine)
    done = invoke(runtime, ["check", str(core)])
    assert done.exit_code == 75 and "load 40 is over 30" in done.stderr and not machine.ran("ruff") and not runtime.providers.clock.slept


def test_fifteen_checks_at_once_run_four_and_defer_eleven(tmp_path):
    from steuermaschine_heinzel.native.check_lib import slots

    stack, grants = __import__("contextlib").ExitStack(), []
    with stack:
        for _ in range(15):
            grants.append(stack.enter_context(slots.pool(tmp_path, wait=0.0, demote=lambda: None)))
        assert sum(g.granted for g in grants) == 4 and sum(not g.granted for g in grants) == 11 and all(g.deferred for g in grants if not g.granted)


def many_tests(core, count, owned=True):  # noqa: F811
    """A selection of COUNT test files for a change of src/heinzel/x.py; with `owned` the first, x_test.py, is named for the module."""
    names = [f"tests/unit/t{n}_test.py" for n in range(count)]
    if owned:
        names[0] = "tests/unit/x_test.py"
    selector = SELECTOR.replace('(["tests/unit/x_test.py"] if any(p.startswith("src/") for p in changed) else [])', f"({names!r} if any(p.startswith('src/') for p in changed) else [])")
    return checkout(core, **{"scripts/affected.py": selector}).on("git diff --name-only", "src/heinzel/x.py")


def test_a_selection_over_40_test_files_runs_the_owning_tests_and_defers_the_rest_to_ci(core, rt):  # noqa: F811
    for n in range(41):
        (core / "tests/unit" / f"t{n}_test.py").parent.mkdir(parents=True, exist_ok=True)
        (core / "tests/unit" / f"t{n}_test.py").write_text("")
    machine = many_tests(core, 41)
    runtime = rt(machine)
    done = invoke(runtime, ["--format", "json", "check", str(core)])
    found = report(done)
    assert done.exit_code == 0 and found.ready and found.receipt, done
    assert found.ci_must_run == ["40 of 41 selected test files"] and "deferred to CI: 40 of 41 selected test files" in found.receipt_note
    assert step(found, "tests").status == "WARN" and "1 of 41 selected test file(s) ran, 40 deferred to PR CI (over 40)" in step(found, "tests").detail
    ran = [call for call in machine.ran("pytest")]
    assert any("tests/unit/x_test.py" in call[0] for call in ran) and not [call for call in ran if "tests/unit/t1_test.py" in call[0]]  # the owner ran, the rest did not
    assert machine.ran("ruff") and machine.ran("mypy")  # the cheap checks of the change still did
    receipt = runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)
    assert receipt["deferred"] is True and receipt["ci_must_run"] == ["40 of 41 selected test files"] and (receipt["tests_ran"], receipt["tests_selected"]) == (1, 41)
    stored, _ = verdicts.check_receipt(runtime.providers.store, HEAD)
    assert verdicts.deferred_note(stored) == "1 of 41 test files ran locally; 40 deferred to PR CI (required check)"


def test_a_selection_over_40_test_files_that_no_test_owns_is_exit_75_and_writes_no_receipt(core, rt):  # noqa: F811
    for n in range(41):
        (core / "tests/unit" / f"t{n}_test.py").parent.mkdir(parents=True, exist_ok=True)
        (core / "tests/unit" / f"t{n}_test.py").write_text("")
    machine = many_tests(core, 41, owned=False)
    runtime = rt(machine)
    done = invoke(runtime, ["--format", "json", "check", str(core)])
    assert done.exit_code == 75 and "no test owns a changed module" in done.stdout + done.stderr, done
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    assert not [call for call in machine.ran("pytest") if "tests/unit/t" in call[0]]  # not one of them ran


def test_a_test_that_imports_the_changed_module_owns_it(core, rt):  # noqa: F811
    for n in range(41):
        (core / "tests/unit" / f"t{n}_test.py").parent.mkdir(parents=True, exist_ok=True)
        (core / "tests/unit" / f"t{n}_test.py").write_text("")
    (core / "tests/unit/t7_test.py").write_text("from heinzel.x import X\n")
    machine = many_tests(core, 41, owned=False)
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert found.ready and found.ci_must_run == ["40 of 41 selected test files"]
    assert any("tests/unit/t7_test.py" in call[0] for call in machine.ran("pytest"))


def test_40_test_files_still_run_here(core, rt):  # noqa: F811
    for n in range(40):
        (core / "tests/unit" / f"t{n}_test.py").parent.mkdir(parents=True, exist_ok=True)
        (core / "tests/unit" / f"t{n}_test.py").write_text("")
    machine = many_tests(core, 40)
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert found.ready and not found.ci_must_run and any("tests/unit/x_test.py" in call[0] for call in machine.ran("pytest"))


def test_a_150_file_mechanical_change_is_checked_cheaply_and_ships_without_a_failure(core, rt):  # noqa: F811
    paths = [f"src/heinzel/m{n}.py" for n in range(150)]
    for path in paths:
        (core / path).parent.mkdir(parents=True, exist_ok=True)
        (core / path).write_text("X = 1\n")
    for n in range(41):
        (core / "tests/unit" / f"t{n}_test.py").write_text("")
    paths.append("src/heinzel/x.py")  # the one module whose test (x_test.py) is among the selected files
    machine = many_tests(core, 41).on("git diff --name-only", "\n".join(paths)).on("git ls-files", "\n".join(paths))
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))
    assert found.ready and found.changed == 151 and found.ci_must_run == ["40 of 41 selected test files"]


def test_a_budget_spent_between_steps_exits_75_and_is_never_ready(core, rt):  # noqa: F811
    machine = src_change(core)
    runtime = rt(machine)
    clock = runtime.providers.clock

    def slow_ruff(argv):  # the formatter "takes" 91 s: the budget is gone before the next step starts
        clock.t += 91
        return Result(0, "", "")

    machine.on("ruff format", slow_ruff)
    done = invoke(runtime, ["check", str(core)])
    assert done.exit_code == 75, done
    assert "local budget of 90 s is spent" in done.stderr and "types, hook, parity, web, docs, standards, tests" in done.stderr
    assert split(done.stderr.splitlines()[-1]) == ["sm", "check", str(core), "--extended"]
    assert "the same focused checks more time" in done.stderr
    assert "rerun your original sm head admit command to adopt that receipt" in done.stderr
    assert "retry on a quieter machine" in done.stderr
    assert not machine.ran("mypy") and runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None


@pytest.mark.parametrize(("base", "skipped"), [(True, False), (False, True), (True, True)])
def test_budget_next_is_a_runnable_command_that_preserves_non_receipting_options(core, rt, tmp_path, base, skipped):  # noqa: F811
    wt = core.rename(core.with_name("work tree"))
    selection = tmp_path / "selected tests.json"
    selection.write_text('{"python": ["tests/unit/x_test.py"]}')
    options = []
    if base:
        options += ["--base", "refs/heads/integration"]
    options += ["--sel", str(selection)]
    if skipped:
        options += ["--skip", "web", "--skip", "docs"]
    machine = src_change(wt)
    runtime = rt(machine)
    machine.on("ruff format", lambda argv: (setattr(runtime.providers.clock, "t", runtime.providers.clock.t + 91), Result(0, "", ""))[1])

    done = invoke(runtime, ["--format", "json", "check", str(wt), *options])
    assert done.exit_code == 75, done
    message = json.loads(done.stdout)
    retry = split(message["hint"])
    assert retry == ["sm", "check", str(wt), "--extended", *options]
    assert "An admission receipt requires the default base and every check." in message["error"]
    assert "adopt" not in message["error"]
    assert invoke(runtime, retry[1:]).exit_code == 0
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None


def test_extended_budget_runs_selected_guards_without_the_whole_suite(core, rt, tmp_path, monkeypatch):  # noqa: F811
    sel = tmp_path / "owning.json"
    sel.write_text('{"python": ["tests/unit/x_test.py"]}')
    machine = checkout(core, DOCS).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", "ALL\n")
    runtime = rt(machine)

    def slow_docs(argv):
        runtime.providers.clock.t += 91
        return Result(0, "", "")

    calls = []
    original = check._preflight.gated

    def observe(allow_full, full, timeout):
        calls.append((allow_full, full, timeout))
        return original(allow_full, full, timeout)

    monkeypatch.setattr(check._preflight, "gated", observe)
    machine.on("uv run typos", slow_docs)
    found = report(invoke(runtime, ["--format", "json", "check", str(core), "--extended", "--sel", str(sel)]))
    assert found.ready and found.receipt
    assert step(found, "tests").data["unit_suite"] is False
    assert machine.ran("tests/integration/guides_test.py")
    assert machine.ran("tests/unit/x_test.py") and not machine.ran("unit and not perf")
    assert calls and all(not allow_full and not full and 0 < timeout <= check.cheap.FAST_SECONDS for allow_full, full, timeout in calls)
    receipt = runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)
    assert receipt["head"] == HEAD and receipt["base"] == MAIN


@pytest.mark.parametrize(("elapsed", "rc", "expected"), [(150, 124, 1), (601, 124, 75), (150, 1, 1)])
def test_extended_budget_does_not_accept_unfinished_or_failed_mandatory_work(core, rt, elapsed, rc, expected):  # noqa: F811
    machine = src_change(core)
    runtime = rt(machine)

    def stopped_type_check(argv):
        runtime.providers.clock.t += elapsed
        return Result(rc, "", "")

    machine.on("uv run mypy", stopped_type_check)
    done = invoke(runtime, ["check", str(core), "--extended"])
    assert done.exit_code == expected
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    if expected == 75:
        assert "local budget of 600 s is spent" in done.stderr
        assert "the extended budget is already in use" in done.stderr
    else:
        assert "types:" in done.stdout


@pytest.mark.parametrize("full_option", ["--allow-full", "--full"])
def test_extended_and_full_modes_are_mutually_exclusive(core, rt, full_option):  # noqa: F811
    machine = checkout(core)
    done = invoke(rt(machine), ["check", str(core), "--extended", full_option])
    assert done.exit_code == 2 and "applies to focused checks" in done.stderr
    assert machine.calls == []


def test_extended_refuses_plugin_gallery_checks_before_running_them(core, rt):  # noqa: F811
    (core / ".git").touch()
    machine = checkout(core).on("remote get-url origin", "https://github.com/example/heinzel-plugins.git")
    machine.on("screenshots-check --all --json", '{"ok": true, "stale": [], "failed": []}')
    runtime = rt(machine)

    done = invoke(runtime, ["check", str(core), "--extended"])
    assert done.exit_code == 2 and "--extended applies to Core checks" in done.stderr
    assert machine.ran("remote get-url origin") and not machine.ran("screenshots-check")

    found = report(invoke(runtime, ["--format", "json", "check", str(core)]))
    assert found.ready and not found.receipt
    assert step(found, "gallery").detail.startswith("every screenshot gallery is fresh")
    assert machine.ran("screenshots-check") and not machine.ran("git merge-base")


def test_a_failure_before_the_budget_ends_still_fails_the_run(core, rt):  # noqa: F811
    machine = src_change(core)
    runtime = rt(machine)
    machine.on("ruff format", lambda argv: (setattr(runtime.providers.clock, "t", runtime.providers.clock.t + 91), Result(1, "would reformat\n", ""))[1])
    done = invoke(runtime, ["--format", "json", "check", str(core)])
    found = report(done)
    assert done.exit_code == 1 and not found.ready and [f.step for f in found.failures] == ["ruff"] and found.deferred


def test_a_type_check_stopped_at_the_budgets_end_exits_75_but_one_stopped_at_its_own_limit_fails(core, rt):  # noqa: F811
    machine = src_change(core)
    runtime = rt(machine)
    clock = runtime.providers.clock
    machine.on("uv run mypy", lambda argv: (setattr(clock, "t", clock.t + 91), Result(124, "", ""))[1])  # stopped when the 90 s were up
    assert invoke(runtime, ["check", str(core)]).exit_code == 75
    plain = report(invoke(rt(src_change(core, **{"uv run mypy": Result(124, "", "")})), ["--format", "json", "check", str(core)]))
    assert not plain.ready and [f.step for f in plain.failures] == ["types"] and "ran out of time" in plain.failures[0].message


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


def test_full_failure_evidence_is_private_complete_and_unique(core, rt, tmp_path, monkeypatch):  # noqa: F811
    import tempfile
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    details = "first failure traceback\n" + "worker stack\n" * 24
    machine = checkout(core).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", "ALL\n")
    runtime = rt(machine.on("unit and not perf", Result(1, details, "two failed\n")))
    done = invoke(runtime, ["--format", "json", "check", str(core), "--allow-full"])
    assert done.exit_code == 1 and "first failure traceback" not in done.stdout + done.stderr
    message = report(done).failures[0].message
    path = Path(message.split("; log ", 1)[1].splitlines()[0])
    assert path.read_text() == details + "two failed\n" and path.stat().st_mode & 0o777 == 0o600
    assert path.parent == tmp_path and "| two failed" in message
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None

    ctx = check.cheap.Ctx(wt=core, outdir=tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda value: Path(ctx.log("../../same", value)), ["first", "second"]))
    assert all(p.parent == tmp_path for p in paths)
    assert [p.read_text() for p in paths] == ["first", "second"] and len({path, *paths}) == 3


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
    sel.write_text(json.dumps({"python": ["tests/unit/x_test.py"]}))
    machine = checkout(core).on("git diff --name-only", "pyproject.toml").on("scripts/related_tests.py", "tests/unit/x_test.py\n")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core), "--sel", str(sel)]))
    assert found.ready and step(found, "tests").data["tests"] == ["tests/unit/x_test.py"] and not machine.ran("unit and not perf")
    assert "scripts/related_tests.py narrows the unit tests to 1 file(s)" in step(found, "tests").data["warnings"][0]
    machine = checkout(core).on("git diff --name-only", "pyproject.toml\nsrc/heinzel/x.py").on("scripts/related_tests.py", "ALL\n")
    found = report(invoke(rt(machine), ["--format", "json", "check", str(core)]))  # src/ changed and no test named: fails, as before
    assert not found.ready and [f.step for f in found.failures] == ["tests"] and "no test is selected" in found.failures[0].message and "--sel FILE" in found.failures[0].fix


@pytest.mark.parametrize("collected", [True, False])
def test_named_python_in_grouped_hook_needs_its_own_collected_case(core, rt, tmp_path, collected):  # noqa: F811
    wanted, other = "tests/unit/wanted_test.py", "tests/unit/other_test.py"
    for name in (wanted, other):
        (core / name).write_text("def test_ok(): pass\n")
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"python": [wanted]}))
    cmd = ["uv", "run", "pytest", other, wanted, "-m", "unit"]

    def emit(argv):
        output = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--junitxml=")))
        assert not output.exists() and not output.is_relative_to(core)
        files = [other, wanted] if collected else [other]
        output.write_text("<testsuite>" + "".join(f'<testcase file="{f}" name="test_ok"/>' for f in files) + "</testsuite>")
        return "pytest returned success"

    machine = checkout(core, {**NONE, "guards": [cmd]}).on(" ".join(cmd), emit)
    runtime = rt(machine)
    found = report(invoke(runtime, ["--format", "json", "check", str(core), "--sel", str(sel)]))
    assert found.ready is collected and bool(found.receipt) is collected
    assert bool(runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD)) is collected
    calls = [argv for argv, *_ in machine.calls if "pytest" in argv and wanted in argv]
    assert len(calls) == 1
    if not collected:
        assert "hook collected no case from" in found.failures[0].message


def test_named_python_files_cannot_disappear_from_a_receipt(core, rt, tmp_path):  # noqa: F811
    sel = tmp_path / "sel.json"
    sel.write_text(json.dumps({"python": ["tests/unit/x_test.py", "tests/unit/missing_test.py"]}))
    machine = checkout(core).on("git diff --name-only", "src/heinzel/x.py")
    runtime = rt(machine)
    found = report(invoke(runtime, ["--format", "json", "check", str(core), "--sel", str(sel)]))
    assert not found.ready and "missing_test.py" in next(f.message for f in found.failures if f.step == "tests")
    assert runtime.providers.store.get_receipt(verdicts.CHECKS, HEAD) is None
    sel.write_text(json.dumps({"python": ["tests/unit/x_test.py::missing_case"]}))
    done = invoke(runtime, ["check", str(core), "--sel", str(sel)])
    assert done.exit_code == 2 and "individual pytest node IDs" in done.stderr


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


def test_concurrent_checks_keep_pytest_cleanup_private(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor
    from pathlib import Path
    from threading import Barrier

    monkeypatch.setenv("PYTEST_DEBUG_TEMPROOT", str(tmp_path))
    case = tmp_path / "test_child.py"
    case.write_text("import os\nfrom pathlib import Path\n"
                    "def test_fixture(tmp_path):\n"
                    "    assert Path(os.environ['PYTEST_DEBUG_TEMPROOT']) in tmp_path.parents\n"
                    "    assert os.environ['CHECK_CASE'] == 'inherited'\n")
    sentinel = tmp_path / "keep"
    sentinel.touch()
    overlap = Barrier(2)

    def subprocess_gate(cmd, cwd, env):
        result = subprocess.run(cmd, cwd=cwd, env=os.environ | env, text=True, capture_output=True, timeout=30)
        return result.returncode, result.stdout + result.stderr

    def check_worker():
        with check.isolated_gate(subprocess_gate) as gate:
            def call(*args):
                return gate([sys.executable, *args], cwd=tmp_path, env={"CHECK_CASE": "inherited", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
            root = Path(call("-c", "import os; print(os.environ['PYTEST_DEBUG_TEMPROOT'])")[1].strip())
            overlap.wait(timeout=15)
            rc, output = call("-m", "pytest", "-q", "-p", "no:cacheprovider", str(case))
            assert rc == 0, output
            assert list(root.glob("pytest-of-*/pytest-*/test_fixture*"))
            assert call("-c", "import sys; print('failed child'); sys.exit(7)") == (7, "failed child\n")
        return root

    with ThreadPoolExecutor(max_workers=2) as executor:
        roots = list(executor.map(lambda _: check_worker(), range(2)))
    assert roots[0] != roots[1]
    assert all(root.parent == tmp_path and not root.exists() for root in roots)
    assert sentinel.exists()
