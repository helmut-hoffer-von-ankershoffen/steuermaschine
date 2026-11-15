"""`sm head preflight`: the moved tests of the former head preflight, run through the command tree. Every external
command is a fake; no network, no git, no gh.

Moved from the former tool's tests. The test bodies and assertions are unchanged except where they named an old command
or the hosted step (now `sm head validate`); each such place is marked `# was:`. The helpers `go`, `result` and the
fake's `key` changed with the edge: the checks get their fakes through `preflight.bound` instead of a `sh` argument."""
import json
from pathlib import Path

import policy_kit as kit
import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import native as native_pkg
from steuermaschine_heinzel.native import preflight
from steuermaschine_heinzel.native.preflight_lib import head as m
from steuermaschine_heinzel.plugin import plugin

REPO = "example/core"
LAST: list = []  # the last invocation, for `result`
HEAD, MAIN, THROWAWAY = "a" * 40, "b" * 40, "c" * 40
MISSPELT_THE, MISSPELT_RECEIVE = "te" + "h", "rec" + "ieve"  # the misspellings of the moved tests, spelled so that this repository's own typos gate passes
PYPROJECT = '[project]\nname = "heinzel"\ndependencies = ["rich>=14", "typer>=0.23"]\n[project.optional-dependencies]\nai = ["mcp>=2"]\n'
CI = """jobs:
  consumer-plan:
    steps:
      - uses: actions/checkout@v7
        with:
          repository: someone/heinzel-plugins
          ref: main
  consumer-check:
    name: Consumer
    steps:
      - uses: actions/checkout@v7
        with:
          repository: someone/heinzel-plugins
          ref: 1234567890abcdef1234567890abcdef12345678 # pinned
  stop-after:
    name: x
"""
BLOCK = "\n".join(f"  const value{i} = await page.locator('#item-{i}').first().textContent({{ timeout: 1000 }});" for i in range(12))


class Fake:
    """Routes a command to a canned (exit, output); records every call."""

    def __init__(self, wt, **over):
        self.wt, self.calls, self.cwds, self.over = wt, [], [], over
        self.changed = over.pop("changed", ["web/src/a.ts"])
        self.deleted = over.pop("deleted", [])  # listed by git diff unless --diff-filter=d leaves deletions out
        self.diff = over.pop("diff", "")

    def __call__(self, cmd, cwd=None, input=None):
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)
        self.cwds.append(cwd)
        key = self.key(cmd)
        value = self.over.get(key, self.default(key, cmd))
        return value(cmd) if callable(value) else value

    @staticmethod
    def key(cmd):
        name = cmd[0].rsplit("/", 1)[-1]
        if name == "git":
            rest = cmd[3:] if cmd[1] == "-C" else cmd[1:]
            return "git " + " ".join(rest[:2] if rest[0] in ("worktree", "push") else rest[:1])
        if name == "gate":  # was: hz-gate
            return "gate " + " ".join(cmd[1:3])
        return name + (" " + cmd[1] if name in ("uv", "gh") else "")

    def default(self, key, cmd):
        if key == "git show" and cmd[-1].endswith(":scripts/release_policy.json"):  # Core's release policy at the head
            return 0, kit.encode(kit.manifest()).decode()
        if key == "git rev-parse":
            return 0, ("feat/thing" if "--abbrev-ref" in cmd else {"HEAD": HEAD, m.BASE: MAIN}[cmd[-1]]) + "\n"
        if key == "git diff":
            if "-U0" in cmd:
                return 0, self.diff
            paths = cmd[cmd.index("--") + 1:]
            listed = self.changed + ([] if "--diff-filter=d" in cmd else self.deleted)
            return 0, "\n".join(p for p in listed if not paths or any(p.startswith(x) for x in paths)) + "\n"
        return {"git status": (0, ""), "git rev-list": (0, "2\n"), "git merge-base": (0, MAIN + "\n"),
                "git ls-remote": (0, f"{MAIN}\trefs/heads/main\n"), "git log": (0, "fix(web): thing\n\nbody\n"),
                "git show": (0, PYPROJECT), "git ls-files": (0, "\n".join(self.changed) + "\n"),
                "git commit-tree": (0, THROWAWAY + "\n"), "gh api": (0, json.dumps({"check_suites": [{"head_branch": "feat/thing"}]})),
                "meta": (0, "commit meta PASS (2 commits)\n"),  # was: hz-preflight-ci-meta
                "scoped-cov": (0, "src/x.py: 95.00% (19/20) missing lines [3] missing branches []\nSCOPED 95.000% (19/20) threshold 90\n"),  # was: hz-scoped-cov
                }.get(key, (0, ""))

    def ran(self, key):
        return [c for c in self.calls if self.key(c) == key]


@pytest.fixture
def core(tmp_path):
    wt = tmp_path / "core"
    for path, text in {"pyproject.toml": PYPROJECT, ".github/workflows/ci.yml": CI, "web/src/a.ts": "export const a = 1;\n",
                       "scripts/preflight_standards.txt": "tests/unit/standards_test.py\n", "scripts/related_tests.py": "", "tests/unit/standards_test.py": "",
                       ".sonarcloud.properties": "sonar.cpd.exclusions=web/src/i18n.ts,tests/unit/public_api_test.py\n"}.items():
        (wt / path).parent.mkdir(parents=True, exist_ok=True)
        (wt / path).write_text(text)
    return wt


def runtime(tmp):
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"repo": REPO}}, tmp)
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers())


SEEN: list[bytes] = []  # the selection bytes scoped coverage read, per run


def call(core, fake, argv, fmt="json"):
    """`sm head preflight ARGV` with every edge of the checks answered by the fake."""
    def bound(rt, worktree, allow_full=False):
        m.bind("origin/main")
        return m.Ctx(wt=worktree, sh=fake, gate=lambda cmd, cwd=None, env=None: fake(["gate", *cmd], cwd=cwd), meta=lambda c: fake(["meta", c.branch or c.sha], cwd=c.wt),
                     scoped=lambda wt, sel: SEEN.append(Path(sel).read_bytes()) or fake(["scoped-cov", wt, sel], cwd=wt),
                     gh=lambda args: fake(["gh", *args]), repo=REPO)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(preflight, "bound", bound)
        patch.setattr(native_pkg, "MODULES", [*[mod for mod in native_pkg.MODULES if mod is not preflight], preflight])
        done = invoke(runtime(core.parent), ["--format", fmt, "head", "preflight", *argv])
    LAST[:] = [done]
    return done


def selection(core, sel):
    path = core.parent / "packet" / "SEL.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(sel))
    return path


def go(core, fake, *args, sel=None):
    argv = [str(core), "--out", str(core.parent / "out"), *args]
    if sel is not None:
        argv = [str(core), "--sel", str(selection(core, sel)), *args]
    return argv, call(core, fake, argv).exit_code


def result(capsys):
    return json.loads(LAST[-1].stdout)


def step(data, name):
    return next(s for s in data["steps"] if s["name"] == name)


# ---- pure helpers ----------------------------------------------------------------------------------------------

def test_added_lines_reads_new_side_ranges_and_ignores_deletions():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,0 +2,3 @@\n+a\n@@ -9 +12 @@\n+b\n@@ -20,2 +22,0 @@\n--- a/gone.py\n+++ /dev/null\n@@ -1,4 +0,0 @@\n"
    assert m.added_lines(diff) == {"x.py": {2, 3, 4, 12}}


def test_dup_estimate_counts_new_lines_inside_a_block_that_exists_twice():
    files = {"web/e2e/a.mjs": "import x from 'y';\n" + BLOCK + "\n", "web/e2e/b.mjs": "// header\n\n" + BLOCK + "\nexport {};\n"}
    r = m.dup_estimate(files, {"web/e2e/b.mjs": set(range(1, 16))})
    assert (r["new_lines"], r["duplicated"]) == (13, 12)  # the comment and the blank line are not code
    assert r["percent"] == pytest.approx(92.31) and r["files"]["web/e2e/b.mjs"]["twin"].startswith("web/e2e/a.mjs:")
    assert m.dup_estimate(files, {"web/e2e/b.mjs": {15}})["duplicated"] == 0  # only the unique line is new


def test_dup_estimate_needs_enough_tokens_and_ignores_overlap_with_itself():
    short = "\n".join(["}"] * 30)
    assert m.dup_estimate({"a.ts": short, "b.ts": short}, {"a.ts": set(range(1, 31))})["duplicated"] == 0
    same = "\n".join(["call(one, two, three, four, five, six, seven, eight, nine, ten);"] * 14)
    assert m.dup_estimate({"a.ts": same}, {"a.ts": set(range(1, 15))})["duplicated"] == 0
    twice = BLOCK + "\nconst gap = 1;\n" + BLOCK
    assert m.dup_estimate({"a.ts": twice}, {"a.ts": set(range(1, 30))})["duplicated"] == 24


def test_dup_scope_honours_cpd_exclusions():
    ex = m.cpd_exclusions("# c\nsonar.cpd.exclusions=web/src/i18n.ts, tests/unit/*_api_test.py\n")
    assert ex == ["web/src/i18n.ts", "tests/unit/*_api_test.py"]
    assert not m.in_dup_scope("web/src/i18n.ts", ex) and not m.in_dup_scope("tests/unit/public_api_test.py", ex)
    assert m.in_dup_scope("web/e2e/x.mjs", ex) and not m.in_dup_scope("docs/x.md", ex)


def test_dependency_changes_and_consumer_ref():
    assert m.dependency_changes(PYPROJECT, PYPROJECT.replace('"typer>=0.23"', '"typer>=0.23", "x"') + "\n[tool.x]\na = 1\n") == ["dependencies: +x"]
    assert m.dependency_changes(PYPROJECT, PYPROJECT.replace("mcp>=2", "mcp>=3")) == ["optional-dependencies.ai: -mcp>=2", "optional-dependencies.ai: +mcp>=3"]
    assert m.dependency_changes(PYPROJECT, PYPROJECT.replace('"rich>=14", "typer>=0.23"', '"typer>=0.23", "rich>=14"')) == []
    assert m.consumer_ref(CI) == "1234567890abcdef1234567890abcdef12345678" and m.consumer_ref("jobs:\n  lint:\n") is None


def test_e2e_owners_follow_helpers_and_python_support_modules():
    drivers = {"web/e2e/chat.mjs": 'import { w } from "./helpers/wait.mjs";\n', "web/e2e/helpers/wait.mjs": "export const w = 1;\n",
               "web/e2e/footer.mjs": "", "web/e2e/site-footer.mjs": "", "web/e2e/tilt.html": '<script src="./tilt.tsx"></script>',
               "web/e2e/tilt.tsx": "", "web/e2e/orphan.mjs": "", "web/e2e/x.test.mjs": ""}
    journeys = {"tests/e2e/chat_test.py": 'run("e2e/chat.mjs")', "tests/e2e/footer_test.py": 'run("e2e/footer.mjs")',
                "tests/e2e/site_test.py": 'run("e2e/site-footer.mjs")', "tests/e2e/_support.py": 'DRIVER = "e2e/tilt.html"',
                "tests/e2e/tilt_test.py": "from tests.e2e._support import DRIVER", "tests/e2e/other_test.py": "import os"}
    owners, unowned = m.e2e_owners(["web/e2e/helpers/wait.mjs", "web/e2e/footer.mjs", "web/e2e/tilt.tsx", "web/e2e/orphan.mjs", "web/e2e/x.test.mjs"], drivers, journeys)
    assert owners == {"tests/e2e/chat_test.py": ["its driver web/e2e/chat.mjs imports changed helper web/e2e/helpers/wait.mjs"],
                      "tests/e2e/footer_test.py": ["owns changed web/e2e/footer.mjs"],
                      "tests/e2e/tilt_test.py": ["its driver web/e2e/tilt.html imports changed helper web/e2e/tilt.tsx via tests/e2e/_support.py"]}
    assert unowned == ["web/e2e/orphan.mjs"]


def test_missing_journeys_need_a_browser_id_or_a_local_only_reason():
    owners = {"tests/e2e/a_test.py": ["x"], "tests/e2e/b_test.py": ["x"], "tests/e2e/c_test.py": ["x"], "tests/e2e/d_test.py": ["x"]}
    local = m.local_only_of({"tests/e2e/b_test.py": "needs the Mini microphone", "tests/e2e/d_test.py": " "})
    assert local == {"tests/e2e/b_test.py": "needs the Mini microphone"}
    assert m.local_only_of([{"id": "tests/e2e/c_test.py::test_x", "reason": "r"}, {"id": "tests/e2e/z_test.py"}]) == {"tests/e2e/c_test.py": "r"}
    sel = {"browser": ["tests/e2e/a_test.py::test_one"], "macos-browser": ["tests/e2e/c_test.py"]}
    assert m.missing_journeys(owners, sel, local) == ["tests/e2e/d_test.py"]
    assert m.missing_journeys(owners, None, {}) == list(owners)


def test_small_parsers():
    assert m.hosted_lanes(["web/src/a.ts"]) == ["web-1", "web-2"]
    assert m.hosted_lanes(["src/heinzel/modules/system/x.py"]) == ["web-1", "web-2", "cli", "supervised"]
    assert m.hosted_lanes(["src/heinzel/heads/cli/main.py"])[-2:] == ["cli", "supervised"]
    # was: m.validation_branch("feat/web_chat", HEAD): the branch name of a throwaway commit belongs to the validation window now, and is tested there
    assert m.parse_scoped("x\nSCOPED 91.250% (1/2) threshold 90\n") == 91.25 and m.parse_scoped("boom") is None
    suites = {"check_suites": [{"head_branch": "validation/x-1", "app": {"slug": "github-actions"}}, {"head_branch": "feat/x"}, {"head_branch": None}]}
    assert [s["branch"] for s in m.validation_suites(suites)] == ["validation/x-1"]


# ---- whole runs ------------------------------------------------------------------------------------------------

def test_ready_writes_the_summary_and_stays_inside_the_gh_budget(core, capsys):
    fake = Fake(core)
    _, rc = go(core, fake)
    data = result(capsys)
    assert rc == 0 and data["ready"] and data["verdict"] == "READY (opt-in not run: consumers, hosted)"
    assert [s["status"] for s in data["steps"]] == ["WARN" if s == "related" else "PASS" for s in m.STEPS] and data["head"] == HEAD  # the web file: the hook runs more than `related`
    assert json.loads((core.parent / "out" / "preflight-head-aaaaaaaa.json").read_text())["verdict"] == data["verdict"]
    assert len([c for c in fake.calls if c[0] == "gh"]) == 1 <= 6 and data["github_calls"] == 1  # was: gh_calls
    assert fake.ran("gate make lint") and not fake.ran("git push") and not fake.ran("scoped-cov")  # was: also not hz-ci-one; hz-scoped-cov
    assert [c[2:] for c in fake.ran("uv run")] == [["typos", "--force-exclude", "--", "web/src/a.ts"], ["typos", "-"]]  # a web-only head asks no script


def test_text_report_numbers_failures_with_fixes_and_names_skips(core, capsys):
    fake = Fake(core, **{"gate make lint": (1, f"typos: `{MISSPELT_THE}` should be `the`\nmake: *** [lint] Error 1\n"), "git status": (0, " M web/src/a.ts\n")})
    done = call(core, fake, [str(core), "--out", str(core.parent / "out"), "--skip", "dup", "--skip", "meta"], fmt="text")
    rc, out = done.exit_code, done.stdout
    assert rc == 1 and "NOT READY: 2 failure(s) (SKIPPED by flag: meta, dup) (opt-in not run: consumers, hosted)" in out
    assert "  1. tree: working tree is not clean" in out and "  2. lint: `make lint` failed (exit 1)" in out
    assert f"`{MISSPELT_THE}` should be `the`" in out and out.count("     fix: ") == 2 and "[SKIP] dup" in out and "summary: " in out
    assert not fake.ran("meta")  # was: hz-preflight-ci-meta


def test_tree_refuses_a_stale_or_diverged_base(core, capsys):
    _, rc = go(core, Fake(core, **{"git ls-remote": (0, "d" * 40 + "\trefs/heads/main\n"), "git merge-base": (0, "e" * 40 + "\n"), "git rev-list": (0, "0\n")}))
    messages = [f["message"] for f in result(capsys)["failures"]]
    assert rc == 1 and len(messages) == 3 and "not ahead" in messages[0] and "merge-base eeeeeeeee" in messages[1] and "stale, remote main is ddddddddd" in messages[2]
    go(core, Fake(core, **{"git ls-remote": (128, "fatal: offline")}))
    data = result(capsys)
    assert data["ready"] and step(data, "tree")["status"] == "WARN"


def test_typos_in_commit_messages_and_meta_failures_are_reported(core, capsys):
    fake = Fake(core, **{"uv run": lambda cmd: (2, f"error: `{MISSPELT_RECEIVE}` should be `receive`") if cmd[-1] == "-" else (0, ""),
                         "meta": (1, "  - commit: 80 chars > 72: 'feat(web): ...'")})  # was: hz-preflight-ci-meta
    _, rc = go(core, fake)
    data = result(capsys)
    assert rc == 1 and [f["step"] for f in data["failures"]] == ["typos", "meta"]
    assert "commit messages" in data["failures"][0]["message"] and MISSPELT_RECEIVE in data["failures"][0]["message"] and "80 chars > 72" in data["failures"][1]["message"]


def test_coverage_runs_only_for_src_and_reports_margin(core, capsys):
    sel = {"python": ["tests/unit/x_test.py"]}
    fake = Fake(core, changed=["src/heinzel/x.py", "web/src/a.ts"])
    argv, rc = go(core, fake, sel=sel)
    cov = step(result(capsys), "coverage")
    assert rc == 0 and cov["status"] == "PASS" and cov["data"]["margin"] == 5.0 and "margin +5.000 points" in cov["detail"]
    ran = fake.ran("scoped-cov")[0]
    assert ran[1] == str(core) and ran[2] != argv[2] and SEEN[-1] == Path(argv[2]).read_bytes()  # a frozen copy of the bytes select read
    assert (core.parent / "packet" / "preflight-head-aaaaaaaa.json").is_file()  # next to SEL

    go(core, Fake(core, changed=["src/heinzel/x.py"], **{"scoped-cov": (0, "SCOPED 91.200% (912/1000) threshold 90\n")}), sel=sel)
    cov = step(result(capsys), "coverage")
    assert cov["status"] == "WARN" and "only 1.200 points" in cov["data"]["warnings"][0]

    low = "src/heinzel/x.py: 80.00% (8/10) missing lines [4, 9] missing branches []\nSCOPED 89.990% (8999/10000) threshold 90\n"
    _, rc = go(core, Fake(core, changed=["src/heinzel/x.py"], **{"scoped-cov": (0, low)}), sel=sel)
    failure = result(capsys)["failures"][0]
    assert rc == 1 and "89.990 % is below 90 %" in failure["message"] and "missing lines [4, 9]" in failure["message"]

    _, rc = go(core, Fake(core, changed=["src/heinzel/x.py"], **{"scoped-cov": (1, "1 failed")}), sel=sel)
    assert rc == 1 and "gave no result" in result(capsys)["failures"][0]["message"]
    _, rc = go(core, Fake(core, changed=["src/heinzel/x.py"]))
    assert rc == 1 and "no SEL with python tests" in result(capsys)["failures"][0]["message"]


def test_coverage_leaves_deleted_files_out(core, capsys):
    """Bead qint: a head that only deletes Python under src/ has nothing to cover, and a deleted file is not measured."""
    _, rc = go(core, Fake(core, changed=["web/src/a.ts"], deleted=["src/heinzel/old.py"]))
    cov = step(result(capsys), "coverage")
    assert rc == 0 and cov["detail"] == "no Python under src/ changed: scoped coverage does not apply"
    go(core, Fake(core, changed=["src/heinzel/x.py"], deleted=["src/heinzel/old.py"]), sel={"python": ["tests/unit/x_test.py"]})
    assert step(result(capsys), "coverage")["data"]["files"] == ["src/heinzel/x.py"]
    from steuermaschine_heinzel.native.preflight_lib import scoped_cov

    asked = []
    listing = lambda argv, cwd=None: asked.append(argv) or (0, "src/heinzel/x.py\n")  # noqa: E731
    assert scoped_cov.production(listing, core) == ["src/heinzel/x.py"] and "--diff-filter=d" in asked[0]


def test_duplication_step_fails_at_the_bar(core, capsys):
    for name in ("a", "b"):
        (core / "web/e2e").mkdir(parents=True, exist_ok=True)
        (core / f"web/e2e/{name}.mjs").write_text(BLOCK + "\n" + "\n".join(f"const {name}{i} = {i};" for i in range(20)) + "\n")
    (core / "web/src/i18n.ts").write_text(BLOCK + "\n")
    diff = "+++ b/web/e2e/b.mjs\n@@ -0,0 +1,32 @@\n+++ b/web/src/i18n.ts\n@@ -0,0 +1,12 @@\n+++ b/README.md\n@@ -0,0 +1,500 @@\n"
    files = ["web/e2e/a.mjs", "web/e2e/b.mjs", "web/src/i18n.ts", "web/src/a.ts"]
    _, rc = go(core, Fake(core, changed=files, diff=diff, **{"git ls-files": (0, "\n".join(files))}), "--skip", "e2e")
    data = result(capsys)
    dup = step(data, "dup")
    assert rc == 1 and (dup["data"]["new_lines"], dup["data"]["duplicated"], dup["data"]["percent"]) == (32, 12, 37.5)
    assert "web/e2e/b.mjs: 12 lines in 1-12, twin near web/e2e/a.mjs:1" in data["failures"][0]["message"] and "shared helper" in data["failures"][0]["fix"]
    _, rc = go(core, Fake(core, changed=files, diff="+++ b/web/e2e/b.mjs\n@@ -0,0 +13,20 @@\n", **{"git ls-files": (0, "\n".join(files))}), "--skip", "e2e")
    assert rc == 0 and "0 of 20 new code lines duplicated = 0.00 %" in step(result(capsys), "dup")["detail"]


def test_dependency_change_fails_with_the_relock_message(core, capsys):
    (core / "pyproject.toml").write_text(PYPROJECT.replace("rich>=14", "rich>=15"))
    fake = Fake(core)
    _, rc = go(core, fake, "--consumers", "heinzel-example")
    failure = result(capsys)["failures"][0]
    assert rc == 1 and "plugin consumers need a relock first" in failure["message"] and "dependencies: +rich>=15" in failure["message"]
    assert not fake.ran("gate make build")


CONTRACTED = PYPROJECT.replace('"rich>=14"', '"pydantic>=2.11,<3", "rich>=15,<16"')
TOOLKIT = 'PLUGIN_API = {1: {"pydantic": 2, "rich": 15}}\n'


def contract_fake(core, toolkit=TOOLKIT, **over):
    """The base pyproject and the plugins toolkit at the consumer ref, both read through `git show`."""
    def show(cmd):
        if cmd[-1].endswith(":scripts/release_policy.json"):  # Core's release policy at the head
            return 0, kit.encode(kit.manifest()).decode()
        return (0, toolkit) if cmd[-1].endswith(m.CONTRACT_FILE) else (0, CONTRACTED)

    return Fake(core, **{"git show": show, **over})


def test_a_dependency_change_inside_the_plugin_api_contract_needs_no_relock(core, capsys):
    """Bead hh7l: with the consumer ref at or after plugins ADR 0003 a change inside the contract passes."""
    plugins = ["--plugins-repo", str(core.parent / "plugins")]
    (core / "pyproject.toml").write_text(CONTRACTED.replace("typer>=0.23", "typer>=0.24"))
    fake = contract_fake(core)
    _, rc = go(core, fake, *plugins)
    deps = step(result(capsys), "deps")
    assert rc == 0 and deps["status"] == "PASS" and "inside core:plugin-api@1 (pydantic 2, rich 15)" in deps["detail"], deps
    assert fake.ran("git merge-base")[-1][3:] == ["merge-base", "--is-ancestor", m.LOCK_BY_CONTRACT, "1234567890abcdef1234567890abcdef12345678"]
    (core / "pyproject.toml").write_text(CONTRACTED.replace("typer>=0.23", "typer>=0.23", 1).replace('"typer>=0.23"', '"typer>=0.23", "httpx>=0.28"'))
    go(core, contract_fake(core), *plugins)
    deps = step(result(capsys), "deps")
    assert deps["status"] == "WARN" and "new base dependency httpx" in deps["data"]["warnings"][0]


@pytest.mark.parametrize("rich", ["rich>=15", "rich>=15,<17", "rich>=14,<16", "rich"])
def test_the_rich_cap_must_stay_inside_15(core, capsys, rich):
    (core / "pyproject.toml").write_text(CONTRACTED.replace("rich>=15,<16", rich))
    _, rc = go(core, contract_fake(core), "--plugins-repo", str(core.parent / "plugins"))
    failure = result(capsys)["failures"][0]
    assert rc == 1 and "leave the plugin-api contract" in failure["message"] and "outside rich 15" in failure["message"], failure


def test_the_contract_is_judged_only_from_a_readable_toolkit_after_adr_0003(core, capsys):
    (core / "pyproject.toml").write_text(CONTRACTED.replace("typer>=0.23", "typer>=0.24"))
    plugins = ["--plugins-repo", str(core.parent / "plugins")]
    for fake, why in ((contract_fake(core, **{"git merge-base": lambda cmd: (1, "") if "--is-ancestor" in cmd else (0, MAIN + "\n")}), "is not at or after plugins 5eb12e2"),
                      (contract_fake(core, toolkit="PLUGIN_API = dict(x=1)\n"), "no PLUGIN_API literal"),
                      (contract_fake(core), "no plugins clone")):
        _, rc = go(core, fake, *(plugins if "clone" not in why else []))
        failure = result(capsys)["failures"][0]
        assert rc == 1 and "need a relock first" in failure["message"] and why in failure["message"], failure
    assert m.plugin_api("PLUGIN_API = {1: {'pydantic': 2}}\nPLUGIN_API = {}\n") is None and m.plugin_api("def (") is None
    assert m.outside_contract(["pydantic>=2,<3", "not a requirement ((("], {"pydantic": 2, "rich": 15}) == ["rich is no base requirement (the contract provides rich 15)"]


def test_consumer_check_uses_the_pinned_ref_and_a_wheel_of_the_head(core, capsys):
    def build(cmd):
        (core / "dist").mkdir(exist_ok=True)
        (core / "dist" / "heinzel-0.9.40-py3-none-any.whl").write_text("")
        return 0, ""
    fake = Fake(core, **{"gate make build": build})
    _, rc = go(core, fake, "--consumers", "heinzel-example", "--plugins-repo", str(core.parent / "plugins"))
    data = result(capsys)
    work = str(core.parent / "out" / "preflight-consumer-aaaaaaaa")
    assert rc == 0 and data["verdict"] == "READY (opt-in not run: hosted)" and "Consumer (heinzel-example) `make check` green" in step(data, "deps")["detail"]
    assert fake.ran("git worktree add")[0][3:] == ["worktree", "add", "--detach", work, "1234567890abcdef1234567890abcdef12345678"]
    assert fake.ran("gate make check")[0][1:] == ["make", "check", "NAME=heinzel-example", f"CORE={core / 'dist' / 'heinzel-0.9.40-py3-none-any.whl'}"]
    assert fake.ran("git worktree remove")

    fake = Fake(core, **{"gate make check": (2, "sm gate: refusing: `make check` runs the full suite")})  # was: hz-gate: refusing
    _, rc = go(core, fake, "--consumers", "heinzel-example", "--plugins-repo", str(core.parent / "plugins"))  # was: the default clone; there is no default path now
    failure = result(capsys)["failures"][0]
    assert rc == 1 and "--allow-full" in failure["fix"] and fake.ran("git worktree remove")  # cleaned up on failure too; was: HZ_ALLOW_FULL=1
    _, rc = go(core, Fake(core, **{"git cat-file": (1, "")}), "--consumers", "heinzel-example", "--plugins-repo", str(core.parent / "plugins"))
    assert rc == 1 and "fetch origin 1234567890abcdef" in result(capsys)["failures"][0]["fix"]
    _, rc = go(core, Fake(core), "--consumers", "heinzel-example")  # new: no clone configured is a failure of the check with its fix, nothing is run
    assert rc == 1 and "preflight_plugins_repo" in result(capsys)["failures"][0]["fix"]


def test_e2e_step_needs_every_owning_journey_in_sel_or_local_only(core, capsys):
    for path, text in {"web/e2e/chat.mjs": 'import "./wait.mjs";', "web/e2e/wait.mjs": "", "web/e2e/footer.mjs": "",
                       "tests/e2e/chat_test.py": '"e2e/chat.mjs"', "tests/e2e/footer_test.py": '"e2e/footer.mjs"'}.items():
        (core / path).parent.mkdir(parents=True, exist_ok=True)
        (core / path).write_text(text)
    changed = ["web/e2e/wait.mjs", "web/e2e/footer.mjs"]
    _, rc = go(core, Fake(core, changed=changed), sel={"browser": ["tests/e2e/footer_test.py"]})
    data = result(capsys)
    assert rc == 1 and step(data, "e2e")["data"]["missing"] == ["tests/e2e/chat_test.py"]
    assert "tests/e2e/chat_test.py: its driver web/e2e/chat.mjs imports changed helper web/e2e/wait.mjs" in data["failures"][0]["message"]
    _, rc = go(core, Fake(core, changed=changed), sel={"browser": ["tests/e2e/footer_test.py"], "local_only": {"tests/e2e/chat_test.py": "Mini only: needs audio"}})
    data = result(capsys)
    assert rc == 0 and step(data, "e2e")["data"]["local_only"] == {"tests/e2e/chat_test.py": "Mini only: needs audio"}
    side = core.parent / "local.json"
    side.write_text(json.dumps({"tests/e2e/chat_test.py": "Mini only"}))
    _, rc = go(core, Fake(core, changed=changed), "--local-only", str(side), sel={"browser": ["tests/e2e/footer_test.py::test_x"]})
    assert rc == 0 and result(capsys)["ready"]


def test_head_with_validation_check_suites_is_refused(core, capsys):
    suites = {"check_suites": [{"head_branch": "validation/feat-thing-aaaaaaaa", "app": {"slug": "github-actions"}, "status": "completed"}]}
    fake = Fake(core, **{"gh api": (0, json.dumps(suites))})
    _, rc = go(core, fake)  # was: with --hosted
    data = result(capsys)
    assert rc == 1 and "#790" in data["failures"][0]["message"] and "validation/feat-thing-aaaaaaaa" in data["failures"][0]["message"]
    assert "hosted" in data["not_run"] and not data["next"] and not fake.ran("git push")  # was: the hosted step is SKIP, no hz-ci-one
    assert fake.ran("gh api")[0][2] == f"repos/{REPO}/commits/{HEAD}/check-suites?per_page=100"  # was: m.REPO
    _, rc = go(core, Fake(core, **{"gh api": (1, "gh: No commit found for SHA (HTTP 422)")}))
    assert rc == 0 and "not on GitHub yet" in step(result(capsys), "validation")["detail"]
    _, rc = go(core, Fake(core, **{"gh api": (1, "gh: not logged in")}))
    assert rc == 1 and "#790 trap is not excluded" in result(capsys)["failures"][0]["fix"]


def test_ready_names_the_hosted_lanes_for_head_validate_and_pushes_nothing(core, capsys):
    """Was: test_hosted_pushes_a_throwaway_commit_dispatches_lanes_and_deletes_the_branch. The throwaway commit, the lanes
    and the branch deletion are `sm head validate` now (tests of the validation window); the preflight names the lanes."""
    fake = Fake(core, changed=["src/heinzel/heads/cli/x.py", "web/src/a.ts"], **{"scoped-cov": (0, "SCOPED 99.000% (99/100) threshold 90\n")})
    _, rc = go(core, fake, sel={"python": ["tests/unit/x_test.py"]})
    data = result(capsys)
    assert rc == 0 and data["verdict"] == "READY (opt-in not run: consumers, hosted)"
    assert data["hosted_lanes"] == ["web-1", "web-2", "cli", "supervised"] and data["next"] == f"sm head validate {core} --lanes web-1,web-2,cli,supervised"
    assert not fake.ran("git commit-tree") and not [c for c in fake.calls if "push" in c]
    assert not any("--force" in c or "--no-verify" in c for c in fake.calls) and len([c for c in fake.calls if c[0] == "gh"]) <= 6


def test_a_head_without_web_or_e2e_changes_goes_on_to_admission_and_a_failing_head_names_no_next(core, capsys):
    """Was: test_hosted_failure_is_reported_and_the_branch_is_still_deleted (hosted failures are `sm head validate`'s)."""
    fake = Fake(core, changed=["src/heinzel/x.py"])
    done = call(core, fake, [str(core), "--sel", str(selection(core, {"python": ["tests/unit/x_test.py"]}))], fmt="text")
    assert done.exit_code == 0 and done.stdout.splitlines()[-1] == f"next: sm head admit {core}" and "summary: " in done.stdout.splitlines()[-2]
    done = call(core, Fake(core, **{"gate make lint": (1, "boom")}), [str(core), "--out", str(core.parent / "out")], fmt="text")
    assert done.exit_code == 1 and done.stdout.splitlines()[-1].startswith("summary: ") and "next:" not in done.stdout
    web = call(core, Fake(core), [str(core), "--out", str(core.parent / "out")], fmt="text")
    assert web.stdout.splitlines()[-1] == f"next: sm head validate {core} --lanes web-1,web-2"


def test_help_and_usage_errors(core, tmp_path):
    r = call(core, Fake(core), ["--help"], fmt="text")  # was: the old script with --help, and --hosted / the admission note in it
    assert r.exit_code == 0 and "READY" in r.stdout and "sm head validate" in r.stdout and "--skip" in r.stdout
    assert go(tmp_path / "nowhere", Fake(tmp_path))[1] == 2  # was: m.main([...], sh=Fake(...)) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert call(core, Fake(core), [str(core), "--sel", str(bad)]).exit_code == 2
    assert go(core, Fake(core), "--skip", "nothing")[1] == 2 and go(core, Fake(core), "--consumers-target", "all")[1] == 2


def test_smells_js_nested_functions_control_flow_and_await_in_loop():
    js = """export async function run(items) {
  for (const item of items) {
    await send(item); // "await" in a string or comment does not count
  }
  for await (const chunk of stream) { await write(chunk); }
  const all = items.map(async (item) => { await send(item); });
  a(() => {
    b(() => {
      c(() => {
        d(function inner() {
          if (x) { if (y) { if (z) { if (w) { for (;;) { stop(); } } } } }
        });
      });
    });
  });
}
"""
    every = set(range(1, 20))
    found = m.js_smells(js, every)
    assert [(line, rule) for line, rule, _ in found] == [(3, "S9382"), (10, "S2004"), (11, "S134")]
    assert m.js_smells(js, {1, 2, 4}) == []  # only changed lines count
    assert m.js_smells("const s = `for (x) { await y }`;\nwhile (a) { f(async () => { await b; }); }\n", {1, 2}) == []


def test_smells_python_and_the_debt_estimate():
    py = """async def run(items):
    for item in items:
        await send(item)
    if a:
        pass
    elif b:
        pass
    elif c:
        pass
    elif d:
        pass
    elif e:
        for x in y:
            with z:
                try:
                    if deep:
                        pass
                except E:
                    pass

    async def inner():
        await ok()
"""
    assert [(line, rule) for line, rule, _ in m.py_smells(py, set(range(1, 30)))] == [(3, "S9382"), (16, "S134")]
    assert m.py_smells("def broken(:\n", {1}) == []
    result = m.smell_estimate({"web/a.ts": "for (const x of y) {\n  await f(x);\n}\n", "src/b.py": py}, {"web/a.ts": {1, 2, 3}, "src/b.py": set(range(1, 11))})
    assert result["rules"] == ["S9382"] and len(result["findings"]) == 2 and result["new_lines"] == 13
    assert result["debt_minutes"] == 10 and result["percent"] == 2.6
    one = m.smell_estimate({"web/a.ts": "for (const x of y) {\n  await f(x);\n}\n"}, {"web/a.ts": {2}})
    assert one["percent"] > m.DEBT_GATE  # 5 min on one new line


def test_a_full_set_head_has_no_focused_lane_to_validate(core, capsys):
    """Core ADR 0149 e: the merge tier runs the full set for this head, so hosted PR CI is its validation."""
    full = json.dumps({"files": ["Makefile"], "lanes": None, "full": True, "reason": "the selector selects the full set"})
    fake = Fake(core, changed=["web/src/a.ts"], python3=(0, full))
    _, rc = go(core, fake, sel={"python": ["tests/unit/x_test.py"]})
    data = result(capsys)
    assert rc == 0 and data["hosted_lanes"] == []
    assert data["next"] == f"full set (the selector selects the full set): hosted PR CI is the validation; sm head admit {core}"


def test_a_selection_edited_during_the_run_changes_nothing_the_preflight_checks(core, capsys):
    """R3c: select reads the bytes once; coverage gets a frozen copy of them and the summary names their sha256."""
    import hashlib

    path = selection(core, {"python": ["tests/unit/x_test.py"]})
    original = path.read_bytes()

    class Editing(Fake):
        def __call__(self, cmd, cwd=None, input=None):
            if not self.calls:  # the first command after select read the file: B is written now, mid-run
                path.write_text(json.dumps({"python": ["tests/unit/other_test.py"]}))
            return super().__call__(cmd, cwd, input)

    _argv, rc = go(core, Editing(core, changed=["src/heinzel/x.py"]), "--sel", str(path))
    assert rc == 0 and SEEN[-1] == original
    assert result(capsys)["selection_sha256"] == hashlib.sha256(original).hexdigest() != hashlib.sha256(path.read_bytes()).hexdigest()
