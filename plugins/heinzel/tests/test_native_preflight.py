"""`sm head preflight`, `sm head meta` and `sm head coverage` through the whole command tree with their real edges:
the run lock, the one GitHub client, the commit-meta check and scoped coverage, on a scripted machine. Text and json.
No network, no real git, gh or make; the lock files, the terms file and every output live under tmp_path."""

import hashlib
import json
from pathlib import Path

import policy_kit as kit
import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.defaults import Subprocess
from steuermaschine.native import NativeRuntime, using
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import native as native_pkg
from steuermaschine_heinzel.native import preflight
from steuermaschine_heinzel.native.preflight_lib import ci_meta, head, scoped_cov
from steuermaschine_heinzel.plugin import plugin

REPO = "example/core"
HEAD, MAIN = "a" * 40, "b" * 40
PYPROJECT = '[project]\nname = "core"\ndependencies = ["rich>=14"]\n'
SCHEMA = '[tool.commitizen.customize]\nschema_pattern = \'(feat|fix)\\((web|cli)\\): [^\\s].*\'\n'


class Machine:
    """A scripted machine. A rule is a needle found in the command line (the longest matching needle wins) and its
    answer: a Result, text (exit 0) or a function of the argv. Without a rule a command prints nothing and exits 0.
    Every call is recorded as (argv, cwd, env)."""

    def __init__(self, **rules):
        self.calls: list = []
        self.rules = {"git rev-parse HEAD": HEAD, "git rev-parse origin/main": MAIN, "git rev-parse --abbrev-ref HEAD": "feat/thing", "git rev-list --count": "2",
                      "git merge-base": MAIN, "git ls-remote": f"{MAIN}\trefs/heads/main", "git diff --name-only": "web/src/a.ts",
                      "git log --format=%B": "fix(web): thing\n\nbody\n", "git log --format=%s": "fix(web): thing\nfeat(cli): other\n",
                      "git show origin/main:pyproject.toml": PYPROJECT, "git ls-files": "web/src/a.ts", "make lint": "lint ok\n", "ps -o": "1\n",
                      "gh api": json.dumps({"check_suites": [{"head_branch": "feat/thing"}]}),
                      f"git show {HEAD}:scripts/release_policy.json": kit.encode(kit.manifest()).decode(), **rules}

    def on(self, needle, answer):
        self.rules[needle] = answer
        return self

    def run(self, argv, cwd=None, timeout=None, env=None):
        argv = [str(word) for word in argv]
        self.calls.append((argv, cwd, env))
        line = " ".join(argv)
        hits = sorted((needle for needle in self.rules if needle in line), key=len, reverse=True)
        answer = self.rules[hits[0]] if hits else ""
        if callable(answer):
            answer = answer(argv)
        return answer if isinstance(answer, Result) else Result(0, answer, "")

    def passthrough(self, argv, cwd=None, env=None, timeout=None):
        raise AssertionError(f"no preflight command may own the terminal: {argv}")

    def ran(self, needle):
        return [call for call in self.calls if needle in " ".join(call[0])]

    def lines(self):
        return [" ".join(call[0]) for call in self.calls]


@pytest.fixture
def core(tmp_path, monkeypatch):
    """A Core worktree with its pyproject, the core's terms checker (never run: the machine answers for it) and one web file."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # a sandbox: the fixed test load of the run lock is honoured only there
    monkeypatch.delenv("HZ_GATE_HELD", raising=False)
    wt = tmp_path / "core"
    for path, text in {"pyproject.toml": PYPROJECT, "scripts/check_confidentiality.py": "", "scripts/related_tests.py": "", "scripts/preflight_standards.txt": "tests/unit/standards_test.py\n",
                       "tests/unit/standards_test.py": "", "web/src/a.ts": "export const a = 1;\n"}.items():
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text(text)
    (tmp_path / "locks").mkdir()
    (tmp_path / "terms").write_text("# invented words only\nzorblat\n")
    return wt


@pytest.fixture
def rt(tmp_path, monkeypatch):
    monkeypatch.setattr("steuermaschine.runlock.os.getloadavg", lambda: (1.0, 1.0, 1.0))  # a fixed load: the machine's does not decide
    monkeypatch.setattr(native_pkg, "MODULES", [*[mod for mod in native_pkg.MODULES if mod is not preflight], preflight])  # until the area is registered

    def build(machine, **more):
        stack = stack_of(plugin)
        table = {"repo": REPO, "lock_directory": str(tmp_path / "locks"), "lock_history": str(tmp_path / "history.jsonl"),
                 "lock_allow_full_log": str(tmp_path / "allow-full.log")}
        settings, _ = stack.validate({"heinzel": table}, tmp_path)
        settings.update({"preflight_terms_file": str(tmp_path / "terms"), "preflight_output_dir": str(tmp_path / "default-out"),
                         "preflight_plugins_repo": str(tmp_path / "plugins"), **more})
        return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers(commands=machine))

    return build


def native_runtime(runtime):
    return using(NativeRuntime(runtime.providers, runtime.settings, runtime.project, say=lambda text: None))


# ---- head preflight ------------------------------------------------------------------------------------------------

def test_preflight_ready_in_text_reaches_every_edge_the_native_way(core, rt, tmp_path):
    machine = Machine()
    done = invoke(rt(machine), ["head", "preflight", str(core)])
    assert done.exit_code == 0, done
    lines = done.stdout.splitlines()
    assert lines[0] == "sm head preflight aaaaaaaaa (feat/thing) vs origin/main bbbbbbbbb"
    rows = [line.split(":")[0] for line in lines[1:] if line.startswith("  [")][:len(head.STEPS)]
    assert rows == [f"  [{'WARN' if name == 'related' else 'PASS'}] {name}" for name in head.STEPS]  # a web file: the hook runs more than `related`
    assert "  [PASS] meta: commit meta PASS (2 commits)" in lines and "  [PASS] lint: make lint green (includes typos)" in lines
    assert lines[-3] == "READY (opt-in not run: consumers, hosted)"
    summary = tmp_path / "default-out" / "preflight-head-aaaaaaaa.json"  # no --sel, no --out: the setting
    assert lines[-2] == f"summary: {summary}" and lines[-1] == f"next: hosted PR CI runs the lanes web-1,web-2; sm head admit {core}"
    assert "... tree" in done.stderr and "... validation" in done.stderr  # progress is not part of the result
    # the heavy command went through the run lock: in the worktree, with the capped workers, recorded in the history
    (lint, where, env), = machine.ran("make lint")
    assert lint == ["make", "lint"] and where == str(core) and env["VITEST_MAX_WORKERS"] == "8" and env["HZ_GATE_HELD"]
    history = [json.loads(line)["cmd"] for line in (tmp_path / "history.jsonl").read_text().splitlines()]
    assert history[0] == "make lint" and history[1].endswith("tests/unit/standards_test.py")  # the standards tests are heavy commands too
    # commit messages reach typos on standard input, through a file and a shell redirect
    (stdin, where, _), = machine.ran('exec "$@"')
    assert stdin[:3] == ["sh", "-c", 'exec "$@" < "$0"'] and stdin[4:] == ["uv", "run", "typos", "-"] and where == str(core)
    assert machine.ran("uv run typos --force-exclude")[0][0][-2:] == ["--", "web/src/a.ts"]
    # the terms file is handed to the core's own checker, once per text; this code never opens it
    checks = machine.ran("check_confidentiality.py")
    assert [call[0][-1] for call in checks] == ["fix(web): thing", "feat(cli): other", "feat/thing"]
    assert all(call[2]["CONFIDENTIAL_TERMS_FILE"] == str(tmp_path / "terms") and call[1] == str(core) for call in checks)
    # exactly one GitHub read, through the one client, on the configured repository
    assert [call[0] for call in machine.calls if call[0][0] == "gh"] == [["gh", "api", f"repos/{REPO}/commits/{HEAD}/check-suites?per_page=100"]]
    assert not machine.ran("git push") and not machine.ran("commit-tree") and "zorblat" not in done.stdout + done.stderr


def test_preflight_in_json_is_the_model_and_the_summary_document_next_to_the_selection(core, rt, tmp_path):
    sel = tmp_path / "packet" / "SEL.json"
    sel.parent.mkdir()
    sel.write_text(json.dumps({"python": ["tests/unit/x_test.py"], "browser": []}))
    machine = Machine(**{"git diff --name-only": "src/heinzel/x.py"})

    def analysed(argv):
        Path(argv[5]).write_text(json.dumps({"files": {"src/heinzel/x.py": {"executed_lines": list(range(19)), "missing_lines": [3], "executed_branches": [], "missing_branches": []}}}))
        return ""

    machine.on("-m coverage run", "1 passed\n").on("from scripts.focused_coverage import _analyse", analysed)
    done = invoke(rt(machine), ["--format", "json", "head", "preflight", str(core), "--sel", str(sel), "--skip", "typos"])
    assert done.exit_code == 0, done
    report = preflight.HeadPreflight.model_validate(json.loads(done.stdout))
    assert report.ready and report.verdict == "READY (SKIPPED by flag: typos) (opt-in not run: consumers, hosted)" and report.skipped == ["typos"]
    assert report.head == HEAD and report.base == MAIN and report.branch == "feat/thing" and report.github_calls == 1 and report.selection == str(sel)
    assert report.selection_sha256 == hashlib.sha256(sel.read_bytes()).hexdigest()  # R3: what `sm head admit` compares
    assert report.arguments == [str(core), "--sel", str(sel), "--skip", "typos"] and report.command == "sm head preflight" and report.generated_at.endswith(":00")
    assert report.hosted_lanes == [] and report.next == f"sm head admit {core}"  # no web or e2e file changed
    coverage = next(step for step in report.steps if step.name == "coverage")
    assert coverage.status == "PASS" and coverage.data["percent"] == 95.0 and coverage.data["margin"] == 5.0
    written = tmp_path / "packet" / "preflight-head-aaaaaaaa.json"
    assert report.summary_file == str(written) and preflight.HeadPreflight.model_validate(json.loads(written.read_text())) == report
    (run, where, env), = machine.ran("-m coverage run")
    assert run[0] == str(core / ".venv/bin/python") and run[-6:] == ["-n", "0", "-q", "-p", "no:cacheprovider", "tests/unit/x_test.py"] and where == str(core)
    assert env["COVERAGE_FILE"].endswith(".coverage") and env["HZ_GATE_HELD"]  # scoped coverage ran its tests through the run lock
    assert (tmp_path / "packet" / "preflight-head-aaaaaaaa-coverage.log").read_text().endswith("SCOPED 95.000% (19/20) threshold 90\n")


def test_preflight_not_ready_numbers_the_failures_and_quotes_the_captured_output(core, rt, tmp_path):
    machine = Machine(**{"make lint": Result(2, "ruff: F401 unused import\n", "make: *** [lint] Error 1\n"), "git status --porcelain": " M web/src/a.ts\n",
                         "gh api": Result(1, "", "gh: not logged in")})
    done = invoke(rt(machine), ["head", "preflight", str(core), "--out", str(tmp_path / "out")])
    assert done.exit_code == 1 and "NOT READY: 3 failure(s) (opt-in not run: consumers, hosted)" in done.stdout
    assert "  1. tree: working tree is not clean (1 paths, first: M web/src/a.ts)" in done.stdout
    assert "  2. lint: `make lint` failed (exit 2); log " in done.stdout and "       | ruff: F401 unused import\n       | make: *** [lint] Error 1" in done.stdout
    assert "  3. validation: could not read the check suites of aaaaaaaaa" in done.stdout and done.stdout.count("     fix: ") == 3
    assert done.stdout.splitlines()[-1] == f"summary: {tmp_path / 'out' / 'preflight-head-aaaaaaaa.json'}"  # no next command for a head that is not ready
    assert (tmp_path / "out" / "preflight-head-aaaaaaaa-lint.log").read_text() == "ruff: F401 unused import\nmake: *** [lint] Error 1\n"


def test_preflight_refuses_what_is_no_core_worktree_and_unknown_names_before_anything_runs(core, rt, tmp_path):
    machine = Machine()
    for argv in ([str(tmp_path / "nowhere")], [str(core), "--skip", "everything"], [str(core), "--consumers-target", "all"], [str(core), "--sel", str(tmp_path / "missing.json")]):
        done = invoke(rt(machine), ["--format", "json", "head", "preflight", *argv])
        assert done.exit_code == 2 and json.loads(done.stdout)["code"] == 2, argv
    assert machine.calls == []
    assert "--skip takes one of tree, lint, typos, types, standards, related, meta, coverage, dup, smells, deps, e2e, validation; unknown: everything" in invoke(
        rt(machine), ["head", "preflight", str(core), "--skip", "everything"]).stderr


def test_the_consumer_check_is_a_full_suite_the_run_lock_refuses_until_the_coordinator_allows_it(core, rt, tmp_path):
    (core / ".github/workflows").mkdir(parents=True)
    (core / ".github/workflows/ci.yml").write_text("jobs:\n  consumer-check:\n    steps:\n      - with:\n          repository: someone/heinzel-plugins\n          ref: main\n")

    def build(argv):
        (core / "dist").mkdir(exist_ok=True)
        (core / "dist" / "heinzel-1.0.0-py3-none-any.whl").write_text("")
        return ""

    machine = Machine(**{"make build": build})
    argv = ["--format", "json", "head", "preflight", str(core), "--out", str(tmp_path / "out"), "--consumers", "heinzel-example"]
    refused = preflight.HeadPreflight.model_validate(json.loads(invoke(rt(machine), argv).stdout))
    assert not refused.ready and "sm gate: refusing: `make check` runs the full" in refused.failures[0].message and "--allow-full" in refused.failures[0].fix
    assert not machine.ran("make check") and machine.ran("worktree remove --force")  # nothing ran, and the plugins worktree is gone again
    allowed = preflight.HeadPreflight.model_validate(json.loads(invoke(rt(machine), [*argv, "--allow-full"]).stdout))
    wheel = core / "dist" / "heinzel-1.0.0-py3-none-any.whl"
    assert allowed.ready and allowed.verdict == "READY (opt-in not run: hosted)" and allowed.arguments[-1] == "--allow-full"
    (check, where, _), = machine.ran("make check")
    assert check == ["make", "check", "NAME=heinzel-example", f"CORE={wheel}"] and where == str(tmp_path / "out" / "preflight-consumer-aaaaaaaa")
    assert "make check NAME=heinzel-example" in (tmp_path / "allow-full.log").read_text()  # the override is logged
    assert machine.ran("worktree add --detach")[0][0][:3] == ["git", "-C", str(tmp_path / "plugins")]  # the setting, when --plugins-repo is not given


# ---- the edges by themselves ----------------------------------------------------------------------------------------

def test_a_gated_command_runs_in_its_directory_and_its_output_is_captured_not_printed(core, rt, tmp_path, capfd):
    with native_runtime(rt(Subprocess())):
        code, out = preflight.gated()(["sh", "-c", "echo out; echo err >&2; pwd; echo $COVERAGE_FILE; exit 3"], cwd=tmp_path / "locks", env={"COVERAGE_FILE": "data"})
    assert code == 3 and out.splitlines() == ["out", str(tmp_path / "locks"), "data", "err"]
    assert capfd.readouterr() == ("", "") and '"exit": 3' in (tmp_path / "history.jsonl").read_text()


def test_text_for_standard_input_reaches_the_command(core, rt):
    assert preflight.shell(Subprocess())(["cat"], input="fix(web): thing\n\nbody\n") == (0, "fix(web): thing\n\nbody\n")
    assert preflight.shell(Subprocess())(["sh", "-c", "echo $ONE; echo two >&2; exit 4"], env={"ONE": "one", "PATH": "/usr/bin:/bin"}) == (4, "one\ntwo\n")
    assert preflight.shell(Subprocess())(["no-such-command-anywhere"])[0] == 127


def test_the_settings_of_the_area_are_typed_described_and_free_of_home_paths():
    fields = preflight.PreflightSettings.model_fields
    assert set(fields) == {"preflight_base", "preflight_plugins_repo", "preflight_output_dir", "preflight_terms_file", "preflight_standards"}
    assert all(field.description for field in fields.values())
    paths = [name for name in fields if name not in {"preflight_base", "preflight_standards"}]  # the rest default to none: no default names a path of a machine
    assert all(fields[name].default is None and fields[name].annotation == Path | None for name in paths)


# ---- head meta ------------------------------------------------------------------------------------------------------

def test_meta_passes_in_text_and_json(core, rt, tmp_path):
    machine = Machine()
    done = invoke(rt(machine), ["head", "meta", str(core)])
    assert (done.exit_code, done.stdout) == (0, "commit meta PASS (2 commits)\n")
    assert machine.ran("git log --format=%s")[0][0][-1] == f"{MAIN}..feat/thing" and machine.ran("git merge-base")[0][0][-2:] == ["origin/main", "feat/thing"]
    assert all(call[1] == str(core) for call in machine.calls)  # every read happens in the worktree given
    doc = json.loads(invoke(rt(machine), ["--format", "json", "head", "meta", str(core)]).stdout)
    meta = preflight.CommitMeta.model_validate(doc)
    assert (meta.branch, meta.commits, meta.problems, meta.title, meta.pull_request, meta.notes) == ("feat/thing", 2, [], None, None, [])


def test_meta_fails_with_the_exact_fix_per_problem_and_never_shows_a_term(core, rt, tmp_path):
    long = "feat(web): " + "x" * 70
    machine = Machine(**{"git log --format=%s": f"{long}\nUpdate stuff\nfix(web): zorblat\n", "git rev-parse --abbrev-ref HEAD": "Feat/Thing",
                         "--text fix(web): zorblat": Result(1, "text:1: confidential term #1\n", "")})
    done = invoke(rt(machine), ["head", "meta", str(core)])
    lines = done.stdout.splitlines()
    assert done.exit_code == 1 and lines[0] == "commit meta FAIL (4), fix BEFORE pushing:" and lines[-1].startswith("  commit subjects cannot change after a push")
    assert lines[1].startswith("  - commit: 81 chars > 72: ") and "shorten the summary to <= 65 chars" in lines[1]
    assert lines[2].startswith("  - commit: not conventional: 'Update stuff'") and lines[3] == "  - branch: 'Feat/Thing' -> lowercase letters, digits, - _ . / only"
    assert lines[4] == "  - confidential: 'fix(web): zorblat' -> text:1: confidential term #1 -> reword"
    assert "zorblat" not in "".join(lines[:4]) and (tmp_path / "terms").read_text().count("zorblat") == 1  # only the commit's own text shows; the file is untouched
    doc = json.loads(invoke(rt(machine), ["--format", "json", "head", "meta", str(core)]).stdout)
    assert len(preflight.CommitMeta.model_validate(doc).problems) == 4


def test_meta_of_a_pull_request_checks_its_title_and_names_the_reopen(core, rt):
    long = "feat(web): " + "y" * 70
    machine = Machine(**{"gh pr view 628": json.dumps({"title": long, "headRefName": "feat/x", "baseRefName": "main", "commits": [{"messageHeadline": "fix(web): short"}]})})
    done = invoke(rt(machine), ["--format", "json", "head", "meta", str(core), "--pr", "628"])
    meta = preflight.CommitMeta.model_validate(json.loads(done.stdout))
    assert done.exit_code == 1 and meta.pull_request == 628 and meta.title == long and meta.branch == "feat/x" and meta.commits == 1
    assert meta.problems == [f"PR title: 81 chars > 72: '{long}' -> shorten the summary to <= 65 chars (a squash adds ' (#N)') -> edit with `gh pr edit 628 --title ...`, "
                             f"THEN `gh pr close 628 --repo {REPO} && gh pr reopen 628 --repo {REPO}` (reruns use the old event payload)"]
    assert machine.ran("gh pr view")[0][0] == ["gh", "pr", "view", "628", "--repo", REPO, "--json", "title,headRefName,baseRefName,commits"] and not machine.ran("git log")
    gone = invoke(rt(Machine(**{"gh pr view 9": Result(1, "", "gh: Not Found (HTTP 404)")})), ["head", "meta", str(core), "--pr", "9"])
    assert gone.exit_code == 1 and "could not read pull request 9: Not Found (HTTP 404)" in gone.stderr


def test_meta_uses_the_worktrees_own_commit_rules_a_detached_head_and_says_when_no_terms_file_exists(core, rt, tmp_path):
    (core / "pyproject.toml").write_text(PYPROJECT + SCHEMA)
    machine = Machine(**{"git log --format=%s": "fix(web): ok\nfix(kernel): scope the worktree does not allow\n", "git rev-parse --abbrev-ref HEAD": "HEAD"})
    done = invoke(rt(machine, preflight_terms_file=str(tmp_path / "no-such-terms")), ["head", "meta", str(core)])
    lines = done.stdout.splitlines()
    assert done.exit_code == 1 and lines[0] == f"note: no terms file at {tmp_path / 'no-such-terms'}: confidential terms are checked only if the environment supplies them"
    assert lines[1] == "commit meta FAIL (1), fix BEFORE pushing:" and "not conventional: 'fix(kernel): scope the worktree does not allow'" in lines[2]
    assert machine.ran("git log --format=%s")[0][0][-1] == f"{MAIN}..{HEAD}"  # detached: the commit stands for the branch
    (core / "scripts/check_confidentiality.py").unlink()
    assert "confidential: scripts/check_confidentiality.py missing, not checked" in invoke(rt(machine), ["head", "meta", str(core)]).stdout
    assert invoke(rt(machine), ["head", "meta", str(tmp_path)]).exit_code == 2  # not a Core worktree


def test_the_meta_rules_read_the_pattern_of_the_given_clone_and_fall_back(tmp_path):
    (tmp_path / "pyproject.toml").write_text(SCHEMA)
    assert ci_meta.pattern(tmp_path).pattern == r"(feat|fix)\((web|cli)\): [^\s].*" and ci_meta.pattern(tmp_path / "none").pattern == ci_meta.FALLBACK
    (tmp_path / "pyproject.toml").write_text("[tool.commitizen.customize]\nschema_pattern = '('\n")
    assert ci_meta.pattern(tmp_path).pattern == ci_meta.FALLBACK
    assert ci_meta.reopen("7") == "gh pr close 7 && gh pr reopen 7"
    assert ci_meta.confidential(["a"], tmp_path, Machine()) == ["confidential: scripts/check_confidentiality.py missing, not checked"]


# ---- head coverage --------------------------------------------------------------------------------------------------

def coverage_machine(files, **rules):
    def analysed(argv):
        Path(argv[5]).write_text(json.dumps({"files": files}))
        return ""

    return Machine(**{"git diff --name-only": "src/heinzel/x.py\nsrc/heinzel/notes.md\n", "-m coverage run": "2 passed in 0.1s\n",
                      "from scripts.focused_coverage import _analyse": analysed, **rules})


def selection(tmp_path, data='{"python": ["tests/unit/x_test.py", "tests/unit/y_test.py"]}'):
    path = tmp_path / "SEL.json"
    path.write_text(data)
    return path


def test_coverage_in_text_and_json(core, rt, tmp_path):
    files = {"src/heinzel/x.py": {"executed_lines": list(range(17)), "missing_lines": [3], "executed_branches": [[1, 2], [4, 5]], "missing_branches": [[7, 9]]}}
    machine = coverage_machine(files)
    done = invoke(rt(machine), ["head", "coverage", str(core), str(selection(tmp_path))])
    assert done.exit_code == 0, done
    assert done.stdout == "src/heinzel/x.py: 90.48% (19/21) missing lines [3] missing branches [[7, 9]]\nSCOPED 90.476% (19/21) threshold 90\n"
    (run, where, env), = machine.ran("-m coverage run")
    assert run == [str(core / ".venv/bin/python"), "-m", "coverage", "run", f"--rcfile={core / 'pyproject.toml'}", "-m", "pytest", "-n", "0", "-q", "-p", "no:cacheprovider",
                   "tests/unit/x_test.py", "tests/unit/y_test.py"] and where == str(core) and env["HZ_GATE_HELD"]
    (analysis, where, _), = machine.ran("_analyse")
    assert analysis[:2] == [str(core / ".venv/bin/python"), "-c"] and analysis[3:5] == [str(core), '["src/heinzel/x.py"]'] and analysis[6] == env["COVERAGE_FILE"]
    assert machine.ran("git diff")[0][0] == ["git", "diff", "--name-only", "--diff-filter=d", "origin/main", "HEAD", "--", "src"]
    doc = json.loads(invoke(rt(machine), ["--format", "json", "head", "coverage", str(core), str(selection(tmp_path))]).stdout)
    found = preflight.ScopedCoverage.model_validate(doc)
    assert found.applies and found.percent == 90.476 and found.threshold == 90 and found.code == 0 and len(found.files) == 2 and found.output == []


def test_coverage_below_the_threshold_without_a_result_and_without_a_src_change(core, rt, tmp_path):
    low = {"src/heinzel/x.py": {"executed_lines": [1, 2, 3, 4], "missing_lines": [5], "executed_branches": [], "missing_branches": []}}
    below = invoke(rt(coverage_machine(low)), ["head", "coverage", str(core), str(selection(tmp_path))])
    assert below.exit_code == 1 and below.stdout.splitlines()[-1] == "SCOPED 80.000% (4/5) threshold 90"
    red = invoke(rt(coverage_machine(low, **{"-m coverage run": Result(1, "FAILED tests/unit/x_test.py::test_one\n1 failed\n", "")})), ["head", "coverage", str(core), str(selection(tmp_path))])
    assert red.exit_code == 1 and "FAILED tests/unit/x_test.py::test_one" in red.stdout and "scoped coverage gave no result (exit 1)" in red.stdout.splitlines()[-1]
    idle = Machine(**{"git diff --name-only": "docs/x.md\n"})
    none = invoke(rt(idle), ["head", "coverage", str(core), str(selection(tmp_path))])
    assert (none.exit_code, none.stdout) == (0, "no Python under src/ changed: scoped coverage does not apply\n") and not idle.ran("coverage")
    bad = invoke(rt(coverage_machine(low)), ["head", "coverage", str(core), str(selection(tmp_path, "{"))])
    assert bad.exit_code == 2 and "unreadable selection" in bad.stderr


def test_the_coverage_arithmetic_counts_lines_and_branch_arcs_of_whole_files():
    rep = {"files": {"src/a.py": {"executed_lines": [1, 2, 3], "missing_lines": [4], "executed_branches": [[1, 2]], "missing_branches": []},
                     "src/empty.py": {"executed_lines": [], "missing_lines": [], "executed_branches": [], "missing_branches": []}}}
    lines, covered, total = scoped_cov.report(rep)
    assert lines == ["src/a.py: 80.00% (4/5) missing lines [4] missing branches []", "src/empty.py: 100.00% (0/0) missing lines [] missing branches []"]
    assert (covered, total) == (4, 5) and scoped_cov.scoped_line(covered, total) == "SCOPED 80.000% (4/5) threshold 90"
    assert head.parse_scoped(scoped_cov.scoped_line(covered, total)) == 80.0
