# ruff: noqa: F811  (a fixture imported from the manifest tests is also a test parameter)
"""`sm head manifest` against a merge-tier base (Core ADR 0149): WHOLE_SUITES, the new lanes, the base's own
focused_lanes, fail-open instead of refusal, Node and advisory JUnit. A scratch Core-shaped repository, no network."""

import json

import pytest
from test_native_packet_manifest import AFFECTED, BOUNDED, JOB, _git, _write, core, generate, run  # noqa: F401  (core: a fixture)

from steuermaschine_heinzel.native.packet_lib import focused_manifest as m

PLAN = '''import re
from scripts.focused_job import VITEST_ALL
PATH = re.compile(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*")
LANES = {"python": ("u", "3", "pytest"), "web": ("u", "3", "vitest"), "browser": ("u", "3", "e2e"),
         "desktop": ("u", "3", "node"), "consumers": ("u", "3", "consumer"),
         "e2e-web-1": ("u", "3", "e2e"), "e2e-web-2": ("u", "3", "e2e")}
DESKTOP_ALL = "desktop/ALL"
CONSUMERS_MAIN = "plugins/main"
WHOLE_SUITES = {"web": VITEST_ALL, "desktop": DESKTOP_ALL, "consumers": CONSUMERS_MAIN,
                "e2e-web-1": "tests/e2e/web-1", "e2e-web-2": "tests/e2e/web-2"}
ADVISORY_LANES = frozenset({"consumers"})
PROTECTED_FILES = ("scripts/focused_plan.py", "Makefile", "uv.lock")
'''
# The base selector maps lanes itself: focused_lanes reads them from selector.json's "focused" entry.
FOCUSED = (AFFECTED + '''
def focused_lanes(result, lane_files):
    data = json.loads((pathlib.Path(lane_files["root"]) / "selector.json").read_text()) if "root" in lane_files else {}
    focused = data.get("focused") or {"lanes": None, "reason": "no focused entry"}
    return focused["lanes"], focused["reason"]
''').replace('self.lanes = {', 'self.lanes = {"root": str(root), ')


@pytest.fixture
def tier(core):
    repo = core["repo"]
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"scripts/focused_plan.py": PLAN, "scripts/focused_job.py": JOB + 'VITEST_ALL = "web/ALL"\n',
                  "scripts/affected.py": FOCUSED, "desktop/test/window.test.js": "", "desktop/src/main.js": "x\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "merge-tier base")
    core["base"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    return core


def _focused(core, lanes, reason="focused"):
    _write(core["repo"], {"selector.json": json.dumps({**BOUNDED, "focused": {"lanes": lanes, "reason": reason}})})
    _git(core["repo"], "commit", "-qam", "selector")


def test_policy_reads_whole_suites_names_and_advisory_lanes(tier):
    policy = m.Policy(m.Repo(tier["repo"]), tier["base"])
    assert policy.merge_tier
    assert policy.whole == {"web": "web/ALL", "desktop": "desktop/ALL", "consumers": "plugins/main",
                            "e2e-web-1": "tests/e2e/web-1", "e2e-web-2": "tests/e2e/web-2"}
    assert policy.advisory == frozenset({"consumers"})
    assert "Makefile" in policy.protected


def test_older_base_keeps_the_web_only_sentinel(core):
    policy = m.Policy(m.Repo(core["repo"]), core["base"])
    assert not policy.merge_tier and policy.whole == {} and policy.advisory == frozenset()


def test_the_base_focused_lanes_derive_the_selection(tier, tmp_path, capsys):
    _focused(tier, {"python": ["tests/unit/a_test.py"], "desktop": ["desktop/ALL"], "consumers": ["plugins/main"]})
    desktop = tmp_path / "desktop.xml"
    desktop.write_bytes(b'<testsuite><testcase classname="x" name="a" file="/w/desktop/test/window.test.js"/></testsuite>')
    consumers = tmp_path / "consumers.xml"
    consumers.write_bytes(b'<testsuite><testcase classname="consumers" name="heinzel-a"><failure/></testcase>'
                          b'<testcase classname="consumers" name="heinzel-b"/></testsuite>')
    code, path = generate(tier, "--junit", f"desktop={desktop}", "--junit", f"consumers={consumers}")
    err = capsys.readouterr().err
    assert code == 0, err
    selection = json.loads(path.read_bytes())["manifest"]["selection"]
    assert selection == {"python": ["tests/unit/a_test.py"], "desktop": ["desktop/ALL"], "consumers": ["plugins/main"]}
    assert "merge tier: CI selects the lanes itself" in err and "a protected file changed" not in err
    assert "selection: derived" in err


def test_full_set_from_the_base_selector_needs_a_declared_widening(tier, capsys):
    _focused(tier, None, "the selector selects the full set")
    assert generate(tier)[0] == 1
    assert "the base selector runs the full set: the selector selects the full set" in capsys.readouterr().err


def test_a_missing_lane_fails_open_instead_of_refusing(tier, tmp_path, capsys):
    _write(tier["repo"], {"desktop/src/main.js": "y\n"})
    _git(tier["repo"], "commit", "-qam", "desktop change")
    _focused(tier, {"python": ["tests/unit/a_test.py"]})
    code, _ = generate(tier)
    assert code == 0
    assert "CI runs the FULL set: a desktop change without the desktop unit suite" in capsys.readouterr().err


def test_a_protected_change_says_the_manifest_is_required(tier, capsys):
    _write(tier["repo"], {"Makefile": "all:\n\techo changed\n"})
    _git(tier["repo"], "commit", "-qam", "makefile")
    _focused(tier, {"python": ["tests/unit/a_test.py"]})
    assert generate(tier)[0] == 0
    err = capsys.readouterr().err
    assert "protected execution changed: Makefile -> needs --reviewed-protected-producer" in err
    assert "it is REQUIRED: a protected file changed" in err


@pytest.mark.parametrize(("selection", "message"), [
    ({"python": ["tests/unit/a_test.py"], "desktop": ["desktop/ALL", "desktop/test/window.test.js"]}, "only a desktop lane"),
    ({"python": ["tests/unit/a_test.py"], "web": ["tests/e2e/web-1"]}, "only a e2e-web-1 lane"),
    ({"python": ["tests/unit/a_test.py"], "consumers": ["plugins/heinzel-a"]}, "focused selections require owning"),
    ({"python": ["tests/unit/a_test.py"], "desktop": ["desktop/test/window.js"]}, "focused selections require owning"),
])
def test_whole_suite_tokens_stand_alone_in_their_own_lane(tier, tmp_path, capsys, selection, message):
    declared = tmp_path / "selection.json"
    declared.write_text(json.dumps(selection))
    assert generate(tier, "--selection", str(declared))[0] == 1
    assert message in capsys.readouterr().err


def test_node_cases_are_owned_by_their_file(tier, tmp_path, capsys):
    declared = tmp_path / "selection.json"
    declared.write_text(json.dumps({"python": ["tests/unit/a_test.py"], "desktop": ["desktop/test/window.test.js"]}))
    report = tmp_path / "desktop.xml"
    report.write_bytes(b'<testsuite><testcase classname="desktop/test/window.test.js" name="opens"/></testsuite>')
    assert generate(tier, "--selection", str(declared), "--junit", f"desktop={report}")[0] == 0
    report.write_bytes(b'<testsuite><testcase classname="c" name="o" file="/w/desktop/test/other.test.js"/></testsuite>')
    assert generate(tier, "--selection", str(declared), "--junit", f"desktop={report}", out="p2.json")[0] == 1
    assert "outside the selection" in capsys.readouterr().err


def test_failures_count_only_in_an_advisory_lane():
    data = b'<testsuite><testcase classname="consumers" name="a"><failure/></testcase></testsuite>'
    assert m.junit_cases(data, advisory=True) == [("consumers", "a")]
    with pytest.raises(m.ManifestError, match="failed"):
        m.junit_cases(data)
    with pytest.raises(m.ManifestError, match="skipped"):
        m.junit_cases(data.replace(b"<failure/>", b"<skipped/>"), advisory=True)


def test_unmet_mirrors_the_core_fail_open_rules():
    assert m.unmet({"web/src/a.ts": "M"}, {}, {"web": ["web/ALL"], "e2e-web-1": ["tests/e2e/web-1"]}) is None
    assert m.unmet({"web/src/a.ts": "M"}, {}, {"web": ["web/ALL"]}) == "web production without Vitest and browser selections"
    assert m.unmet({}, {"src/heinzel/a.py": "b"}, {}) == "Python production without a Python selection"
    many = {f"src/heinzel/m{i}.py": "b" for i in range(101)}
    assert "exceed the scoped coverage bound" in m.unmet({}, many, {"python": ["x"]})
    assert m.unmet({"desktop/README.md": "M", "desktop/managed/x.ts": "M"}, {}, {}) is None


def test_unsupported_base_definitions_refuse(tier):
    with pytest.raises(m.ManifestError, match="base defines no NOPE"):
        m._constant(m.ast.parse("NOPE").body[0].value)
    with pytest.raises(m.ManifestError, match="not a string"):
        m._constant(m.ast.parse("1").body[0].value)
    with pytest.raises(m.ManifestError, match="ADVISORY_LANES"):
        m._literal_set(m.ast.parse("frozenset(x)").body[0].value, "ADVISORY_LANES")
    with pytest.raises(m.ManifestError, match="WHOLE_SUITES"):
        m._whole_suites({"WHOLE_SUITES": m.ast.parse("dict()").body[0].value}, {})
    assert run(tier, "--verify", str(tier["tmp"] / "missing.json")) == 1


def test_execution_pins_the_protected_patterns_one_level_deep(tier, capsys):
    repo = tier["repo"]
    _write(repo, {"scripts/focused_plan.py": PLAN + 'PROTECTED_PATTERNS = ("tests/_*.py", "tests/e2e/_*.py")\n',
                  "tests/_helper.py": "a\n", "tests/e2e/_driver.py": "b\n", "tests/unit/_deep.py": "c\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "patterns")
    tier["base"] = _git(repo, "rev-parse", "HEAD")
    _write(repo, {"tests/_helper.py": "changed\n"})
    _git(repo, "commit", "-qam", "helper")
    _focused(tier, {"python": ["tests/unit/a_test.py"]})
    code, path = generate(tier)
    assert code == 0
    execution = json.loads(path.read_bytes())["manifest"]["execution"]
    assert set(execution) == {"scripts/focused_plan.py", "Makefile", "uv.lock", "tests/_helper.py", "tests/e2e/_driver.py"}
    assert "protected execution changed: tests/_helper.py" in capsys.readouterr().err
    _git(repo, "rm", "-q", "tests/e2e/_driver.py")
    _git(repo, "commit", "-qm", "drop driver")
    assert generate(tier, out="p3.json")[0] == 0
    assert "tests/e2e/_driver.py -> needs --reviewed-protected-producer" in capsys.readouterr().err


# The base's own protected_blobs (Round 3): every PROTECTED_FILES path plus any conftest.py anywhere in the tree.
PROTECTED_BLOBS = '''
def protected_blobs(commit):
    import subprocess
    out = subprocess.run(["git", "ls-tree", "-r", "-z", commit], capture_output=True, check=True).stdout
    blobs = {}
    for entry in filter(None, out.split(b"\\0")):
        meta, name = entry.split(b"\\t", 1)
        name = name.decode()
        if name in PROTECTED_FILES or name.rsplit("/", 1)[-1] == "conftest.py":
            blobs[name] = meta.split()[2].decode()
    return blobs
'''


def test_execution_is_the_base_protected_blobs_of_the_tree(tier, capsys):
    repo = tier["repo"]
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"scripts/focused_plan.py": PLAN + PROTECTED_BLOBS, "web/src/deep/conftest.py": "a\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "protected_blobs base")
    tier["base"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _write(repo, {"web/src/deep/conftest.py": "changed\n", "tests/unit/conftest.py": "new\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "conftests")
    _focused(tier, {"python": ["tests/unit/a_test.py"]})
    code, path = generate(tier)
    err = capsys.readouterr().err
    assert code == 0, err
    execution = json.loads(path.read_bytes())["manifest"]["execution"]
    assert set(execution) == {"scripts/focused_plan.py", "Makefile", "uv.lock", "web/src/deep/conftest.py", "tests/unit/conftest.py"}
    assert execution["tests/unit/conftest.py"] == _git(repo, "rev-parse", "feature:tests/unit/conftest.py")
    assert "protected execution changed: web/src/deep/conftest.py, tests/unit/conftest.py ->" in err or \
        "protected execution changed: tests/unit/conftest.py, web/src/deep/conftest.py ->" in err


def test_a_failing_base_protected_blobs_refuses(tier, capsys):
    repo = tier["repo"]
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"scripts/focused_plan.py": PLAN + "def protected_blobs(commit):\n    raise RuntimeError('nope')\n"})
    _git(repo, "commit", "-qam", "broken base")
    tier["base"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    _focused(tier, {"python": ["tests/unit/a_test.py"]})
    assert generate(tier)[0] == 1
    assert "base focused_plan.protected_blobs failed" in capsys.readouterr().err
