"""hz-focused-manifest: deterministic packets from a scratch Core-shaped Git repository (no network, no registry)."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from packet_paths import TOOLS

# (moved: the package is imported, no path insert)
from steuermaschine_heinzel.native.packet_lib import focused_manifest as m

PLAN = '''import re
PATH = re.compile(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*")
LANES = {"python": ("u", "3", "pytest"), "web": ("u", "3", "vitest"), "browser": ("u", "3", "e2e")}
PROTECTED_FILES = ("scripts/focused_plan.py", "uv.lock")
'''
JOB = '''import re
MAX_IDS = 2
PYTHON_CI_MAX_IDS = 3
TOKEN = re.compile(r"(?![^:]*\\/-)\\w[\\w./-]{0,199}(::\\w{1,120}(\\[[\\w.,=+-]{1,80}\\])?){0,2}", re.ASCII)
'''
# A stand-in for Core's selector with the same interface the driver uses: the result comes from a file in the tree.
AFFECTED = '''import argparse, ast, json, pathlib, subprocess
class Repo:
    def __init__(self, root):
        self.root = root
        self.lanes = {"web-1": ["tests/e2e/web_ui_test.py"], "web-2": [], "cli": ["tests/e2e/cli_test.py"]}
def classify(repo, changed):
    result = json.loads((repo.root / "selector.json").read_text())
    result["files"] = sorted(changed)
    return result
'''
BOUNDED = {"full": False, "pytest": ["tests/unit/a_test.py"], "vitest": [], "e2e_lanes": [], "jobs": ["lint"],
           "reasons": ["1 python source file(s)"]}
JUNIT = (b'<testsuites><testsuite><testcase classname="tests.unit.a_test" name="test_one"/>'
         b'<testcase classname="tests.unit.a_test.TestX" name="test_two"/></testsuite></testsuites>')


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_AUTHOR_DATE": "1700000000 +0000",
            "GIT_COMMITTER_DATE": "1700000000 +0000"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def core(tmp_path):
    repo = tmp_path / "core"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")  # a packet base must be on origin/main
    _write(repo, {"scripts/focused_plan.py": PLAN, "scripts/focused_job.py": JOB, "scripts/affected.py": AFFECTED,
                  "scripts/related_tests.py": "", "uv.lock": "lock\n", "pyproject.toml": "[project]\n",
                  "Makefile": "all:\n", "selector.json": json.dumps(BOUNDED), "src/heinzel/mod.py": "x = 1\n",
                  "tests/unit/a_test.py": "def test_one(): pass\n", "tests/unit/b_test.py": "", "tests/unit/c_test.py": "",
                  "tests/unit/d_test.py": "", "web/src/x.test.ts": "", "tests/e2e/web_ui_test.py": ""})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    _write(repo, {"src/heinzel/mod.py": "x = 2\n", "tests/unit/a_test.py": "def test_one(): pass\ndef test_two(): pass\n"})
    _git(repo, "commit", "-qam", "change")
    junit = tmp_path / "python.xml"
    junit.write_bytes(JUNIT)
    return {"repo": repo, "base": base, "head": _git(repo, "rev-parse", "HEAD"), "junit": junit, "tmp": tmp_path}


def run(core, *args):
    return m.main(["--core-repo", str(core["repo"]), *args])


def generate(core, *extra, out="packet.json"):
    path = core["tmp"] / out
    code = run(core, "feature", "--base", core["base"], "--junit", f"python={core['junit']}", "-o", str(path), *extra)
    return code, path


def test_derived_packet_matches_the_resolver_contract_and_verifies_byte_equal(core, capsys):
    code, path = generate(core)
    assert code == 0
    packet = json.loads(path.read_bytes())
    pr, main = packet["manifest"], packet["main_manifest"]
    repo, head, base = core["repo"], core["head"], core["base"]
    tree = _git(repo, "rev-parse", "feature^{tree}")
    raw = subprocess.run(["git", "-C", str(repo), "diff-tree", "--no-commit-id", "-r", "--raw", "--no-abbrev",
                          "--no-renames", "-z", base, tree], check=True, capture_output=True).stdout
    assert pr == {
        "schema": 1, "event": "pull_request", "ref": "refs/pull/*/merge", "base": base, "head": head, "tree": tree,
        "changed": {"src/heinzel/mod.py": "M", "tests/unit/a_test.py": "M"},
        "diff_sha256": hashlib.sha256(raw).hexdigest(), "mode": "pr-scoped",
        "production": {"src/heinzel/mod.py": _git(repo, "rev-parse", "feature:src/heinzel/mod.py")},
        "config_blob": _git(repo, "rev-parse", "feature:pyproject.toml"),
        "execution": {p: _git(repo, "rev-parse", f"feature:{p}") for p in ("scripts/focused_plan.py", "uv.lock")},
        "selection": {"python": ["tests/unit/a_test.py"]},
        "case_sha256": {"python": hashlib.sha256(json.dumps(
            sorted([["tests.unit.a_test", "test_one"], ["tests.unit.a_test.TestX", "test_two"]]),
            separators=(",", ":")).encode()).hexdigest()},
    }
    assert main == {**{k: v for k, v in pr.items() if k != "head"}, "event": "push", "ref": "refs/heads/main"}
    assert packet["review"] == {"base": base, "head": head, "tree": tree,
                                "manifest_sha256": hashlib.sha256(m.canonical(pr)).hexdigest(),
                                "main_manifest_sha256": hashlib.sha256(m.canonical(main)).hexdigest()}
    assert path.read_bytes() == (json.dumps(packet, sort_keys=True, indent=2) + "\n").encode()
    assert "selection: derived from the repository selector" in capsys.readouterr().err
    assert run(core, "--verify", str(path)) == 0
    assert "BYTE-EQUAL" in capsys.readouterr().err


def test_review_binding_and_canonical_layout_round_trip(core, capsys):
    (core["tmp"] / "review.md").write_text("APPROVE\n")
    code, path = generate(core, "--review", "review.md", "--review-root", str(core["tmp"]), "--format", "canonical")
    assert code == 0
    data = path.read_bytes()
    assert not data.endswith(b"\n")
    assert json.loads(data)["review"]["sha256"] == hashlib.sha256(b"APPROVE\n").hexdigest()
    assert run(core, "--verify", str(path)) == 0  # review found next to the packet, layout taken from it
    (core["tmp"] / "review.md").write_text("changed\n")
    assert run(core, "--verify", str(path)) == 1
    assert "DIFFERENT review.sha256" in capsys.readouterr().err


@pytest.mark.parametrize("field", ["tree", "changed", "production", "execution", "diff_sha256"])
def test_verify_detects_a_tampered_git_derived_field(core, capsys, field):
    _, path = generate(core)
    packet = json.loads(path.read_bytes())
    packet["manifest"][field] = "0" * 40 if isinstance(packet["manifest"][field], str) else {}
    path.write_bytes(m.packet_bytes(packet))
    assert run(core, "--verify", str(path)) == 1
    assert f"DIFFERENT manifest.{field}" in capsys.readouterr().err


def test_verify_rederives_cases_from_junit(core, tmp_path):
    _, path = generate(core)
    assert run(core, "--verify", str(path), "--junit", f"python={core['junit']}") == 0
    other = tmp_path / "other.xml"
    other.write_bytes(JUNIT.replace(b'name="test_two"', b'name="test_three"'))
    assert run(core, "--verify", str(path), "--junit", f"python={other}") == 1


@pytest.mark.parametrize(("selector", "message"), [
    ({**BOUNDED, "full": True, "pytest": ["ALL"], "reasons": ["Makefile: full"]}, "selects the full set"),
    ({**BOUNDED, "vitest": ["ALL"]}, "selects the full set"),
    ({**BOUNDED, "pytest": ["tests/unit/"]}, "directory selection is broad"),
    ({**BOUNDED, "pytest": [], "e2e_lanes": ["cli"]}, "e2e lane cli has no focused lane mapping"),
    ({**BOUNDED, "pytest": []}, "selects no tests"),
])
def test_broad_or_empty_repository_selection_refuses(core, capsys, selector, message):
    _write(core["repo"], {"selector.json": json.dumps(selector)})
    _git(core["repo"], "commit", "-qam", "selector")
    assert generate(core)[0] == 1
    assert message in capsys.readouterr().err


def test_browser_lane_maps_to_the_makefile_lane_files(core, tmp_path):
    _write(core["repo"], {"selector.json": json.dumps({**BOUNDED, "e2e_lanes": ["web-1"]})})
    _git(core["repo"], "commit", "-qam", "selector")
    browser = tmp_path / "browser.xml"
    browser.write_bytes(b'<testsuite><testcase classname="tests.e2e.web_ui_test" name="test_ui"/></testsuite>')
    code, path = generate(core, "--junit", f"browser={browser}")
    assert code == 0
    assert json.loads(path.read_bytes())["manifest"]["selection"]["browser"] == ["tests/e2e/web_ui_test.py"]


def test_declared_selection_is_validated_and_reported_as_delta(core, tmp_path, capsys):
    _write(core["repo"], {"selector.json": json.dumps({**BOUNDED, "pytest": ["tests/unit/"]})})  # a broad ask a declaration narrows
    _git(core["repo"], "commit", "-qam", "selector")
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"python": ["tests/unit/a_test.py::test_one", "tests/unit/a_test.py::test_two"]}))
    code, _ = generate(core, "--selection", str(selection))
    assert code == 0
    err = capsys.readouterr().err
    assert "selection: DECLARED" in err and "+ python tests/unit/a_test.py::test_one" in err and "(no bounded derivation)" in err
    for bad, message in [({"web": ["web/src/x.test.ts"]}, "Python production requires a Linux coverage selection"),
                         ({"python": ["tests/unit/missing_test.py"]}, "missing or ambiguous tracked file"),
                         ({"python": ["tests/e2e/web_ui_test.py"]}, "browser tests require their owning lane"),
                         ({"nope": ["tests/unit/a_test.py"]}, "unknown focused lane"),
                         ({"python": ["tests/unit/a_test.py", "tests/unit/a_test.py"]}, "duplicate selection")]:
        selection.write_text(json.dumps(bad))
        assert generate(core, "--selection", str(selection))[0] == 1
        assert message in capsys.readouterr().err


def test_declared_selection_that_leaves_out_a_selected_file_is_refused(core, tmp_path, capsys):
    """0rxo: a declared selection may narrow a broad ask, never drop a file the repository selector names."""
    _write(core["repo"], {"selector.json": json.dumps({**BOUNDED, "pytest": ["tests/unit/a_test.py", "tests/unit/b_test.py", "tests/unit/"]})})
    _git(core["repo"], "commit", "-qam", "selector")
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"python": ["tests/unit/a_test.py"]}))
    assert generate(core, "--selection", str(selection))[0] == 1
    err = capsys.readouterr().err
    assert "leaves out 1 test(s) the repository selector asks for" in err and "python tests/unit/b_test.py" in err
    policy = type("P", (), {"whole": {"web": "web/ALL"}})()
    asked = {"pytest": ["tests/unit/", "ALL", "tests/unit/b_test.py"], "vitest": ["web/src/x.test.ts"],
             "e2e_lanes": ["web-1", "cli"], "lane_files": {"web-1": ["tests/e2e/web_ui_test.py"]}}
    assert m.selector_asks_more(asked, {"python": ["tests/unit/b_test.py"], "web": ["web/ALL"]}, policy) == [("browser", "tests/e2e/web_ui_test.py")]
    # R2 (the reviewer's probe): one node id does not stand for the whole named file
    assert ("python", "tests/unit/b_test.py") in m.selector_asks_more(asked, {"python": ["tests/unit/b_test.py::test_x"]}, policy)
    selection.write_text(json.dumps({"python": ["tests/unit/a_test.py::test_one", "tests/unit/b_test.py"]}))
    assert generate(core, "--selection", str(selection))[0] == 1
    assert "python tests/unit/a_test.py" in capsys.readouterr().err


def test_junit_must_match_the_selection(core, tmp_path, capsys):
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"python": ["tests/unit/a_test.py", "tests/unit/b_test.py"]}))
    assert generate(core, "--selection", str(selection))[0] == 1
    assert "no JUnit case for tests/unit/b_test.py" in capsys.readouterr().err
    stray = tmp_path / "stray.xml"
    stray.write_bytes(JUNIT.replace(b"tests.unit.a_test.TestX", b"tests.unit.zz_test"))
    assert run(core, "feature", "--base", core["base"], "--junit", f"python={stray}") == 1
    assert "outside the selection" in capsys.readouterr().err
    assert run(core, "feature", "--base", core["base"]) == 1
    assert "missing ['python']" in capsys.readouterr().err
    skipped = tmp_path / "skipped.xml"
    skipped.write_bytes(JUNIT.replace(b'name="test_two"/>', b'name="test_two"><skipped/></testcase>'))
    assert run(core, "feature", "--base", core["base"], "--junit", f"python={skipped}") == 1


def test_conflicting_merge_refuses(core, capsys):
    repo = core["repo"]
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"src/heinzel/mod.py": "x = 3\n"})
    _git(repo, "commit", "-qam", "conflict")
    assert run(core, "feature", "--base", "main", "--junit", f"python={core['junit']}") == 1
    assert "do not merge cleanly" in capsys.readouterr().err


def test_inherited_git_environment_cannot_redirect_reads(core, monkeypatch, tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "caller-index"))
    assert generate(core)[0] == 0
    assert not (tmp_path / "caller-index").exists()


def test_the_python_lane_has_no_maximum_whatever_the_base_says(core, capsys):
    """Owner 2026-11-14 (bead hl17): the base's PYTHON_CI_MAX_IDS is not read; only the other lanes keep MAX_IDS."""
    repo = core["repo"]
    _git(repo, "checkout", "-q", "main")
    policy = m.Policy(m.Repo(repo), _git(repo, "rev-parse", "HEAD"))
    assert not hasattr(policy, "max_ids") and not hasattr(policy, "python_max_ids")
    _write(repo, {"selector.json": json.dumps({**BOUNDED, "pytest": [f"tests/unit/{n}_test.py" for n in "abcd"]})})
    _git(repo, "commit", "-qam", "selector")
    generate(core)
    assert "broad focused lane python" not in capsys.readouterr().err  # four ids, over the old PYTHON_CI_MAX_IDS = 3


def test_candidate_scripts_never_execute_in_the_selector(core, tmp_path):
    marker = tmp_path / "pwned"
    payload = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\nraise SystemExit(0)\n"
    _write(core["repo"], {f"scripts/{name}.py": payload for name in ("argparse", "ast", "subprocess", "json",
                                                                      "affected", "related_tests")})
    _git(core["repo"], "add", "-A")
    _git(core["repo"], "commit", "-qm", "malicious")
    selector = m.repo_selection(m.Repo(core["repo"]), core["base"], _git(core["repo"], "rev-parse", "feature^{tree}"),
                                {"src/heinzel/mod.py": "M"})
    assert not marker.exists()
    assert selector["pytest"] == ["tests/unit/a_test.py"]


def test_vitest_cases_must_belong_to_selected_files(core):
    cases = [("src/x.test.ts", "works")]
    m.check_case_owners("vitest", ["web/src/x.test.ts"], cases, "web")
    with pytest.raises(m.ManifestError, match="outside the selection"):
        m.check_case_owners("vitest", ["web/src/x.test.ts"], [*cases, ("src/y.test.ts", "other")], "web")
    with pytest.raises(m.ManifestError, match="no JUnit case for web/src/z.test.ts"):
        m.check_case_owners("vitest", ["web/src/x.test.ts", "web/src/z.test.ts"], cases, "web")


# -- the whole Vitest suite: web ["web/ALL"] -----------------------------------------------------------------------

WEB_JUNIT = (b'<testsuites><testsuite><testcase classname="src/x.test.ts" name="x"/>'
             b'<testcase classname="src/y.test.tsx" name="y"/>'
             b'<testcase classname="e2e/z.test.mjs" name="z"/></testsuite></testsuites>')


@pytest.fixture
def web_core(core):
    """Base whose focused_job.py has VITEST_ALL; the tree has three Vitest files and one non-test web file."""
    repo = core["repo"]
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"scripts/focused_job.py": JOB + 'VITEST_ALL = "web/ALL"\n', "web/src/y.test.tsx": "",
                  "web/e2e/z.test.mjs": "", "web/src/app.ts": ""})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "web/ALL base")
    core["base"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    web = core["tmp"] / "web.xml"
    web.write_bytes(WEB_JUNIT)
    core["web"] = web
    return core


def _selector(core, selector):
    _write(core["repo"], {"selector.json": json.dumps(selector)})
    _git(core["repo"], "commit", "-qam", "selector")


@pytest.mark.parametrize("vitest", [["ALL"]])
def test_vitest_all_becomes_the_whole_suite_sentinel(web_core, capsys, vitest):
    _selector(web_core, {**BOUNDED, "vitest": vitest})
    code, path = generate(web_core, "--junit", f"web={web_core['web']}")
    assert code == 0, capsys.readouterr().err
    plan = json.loads(path.read_bytes())["manifest"]
    assert plan["selection"] == {"python": ["tests/unit/a_test.py"], "web": ["web/ALL"]}
    assert plan["case_sha256"]["web"] == m.cases_digest(m.junit_cases(WEB_JUNIT))
    err = capsys.readouterr().err
    assert "web: web/ALL = the whole Vitest suite" in err and "selection: derived" in err
    assert run(web_core, "--verify", str(path), "--junit", f"web={web_core['web']}") == 0


@pytest.mark.parametrize(("junit", "message"), [
    (WEB_JUNIT.replace(b'<testcase classname="e2e/z.test.mjs" name="z"/>', b""), "no JUnit case for 1 file(s): e2e/z"),
    (WEB_JUNIT.replace(b"src/y.test.tsx", b"src/gone.test.tsx"), "outside the tree's Vitest files: src/gone"),
    (WEB_JUNIT.replace(b"src/y.test.tsx", b"y.test.tsx"), "outside the tree's Vitest files: y.test.tsx"),
    (WEB_JUNIT.replace(b'name="y"', b'name="x"').replace(b"src/y.test.tsx", b"src/x.test.ts"), "duplicated"),
])
def test_whole_suite_junit_must_cover_exactly_the_trees_vitest_files(web_core, tmp_path, capsys, junit, message):
    _selector(web_core, {**BOUNDED, "vitest": ["ALL"]})
    report = tmp_path / "bad.xml"
    report.write_bytes(junit)
    assert generate(web_core, "--junit", f"web={report}")[0] == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(("selection", "message"), [
    ({"python": ["tests/unit/a_test.py"], "web": ["web/ALL", "web/src/x.test.ts"]}, "only a web lane on its own"),
    ({"python": ["tests/unit/a_test.py", "web/ALL"]}, "only a web lane on its own"),
    ({"python": ["tests/unit/a_test.py"], "browser": ["web/ALL"]}, "only a web lane on its own"),
    ({"python": ["tests/unit/a_test.py"], "web": ["web/ALL", "web/ALL"]}, "duplicate selection"),
    ({"python": ["tests/unit/a_test.py"], "web": ["ALL"]}, "invalid focused test"),
])
def test_declared_whole_suite_is_refused_mixed_or_outside_the_web_lane(web_core, tmp_path, capsys, selection, message):
    declared = tmp_path / "selection.json"
    declared.write_text(json.dumps(selection))
    assert generate(web_core, "--selection", str(declared), "--junit", f"web={web_core['web']}")[0] == 1
    assert message in capsys.readouterr().err


def test_base_without_vitest_all_keeps_refusing_the_whole_suite(core, tmp_path, capsys):
    _selector(core, {**BOUNDED, "vitest": ["ALL"]})
    assert generate(core)[0] == 1
    assert "selects the full set" in capsys.readouterr().err
    declared = tmp_path / "selection.json"
    declared.write_text(json.dumps({"python": ["tests/unit/a_test.py"], "web": ["web/ALL"]}))
    assert generate(core, "--selection", str(declared))[0] == 1
    assert "focused selections require owning test files" in capsys.readouterr().err


def test_a_different_base_sentinel_refuses(core):
    repo = core["repo"]
    _write(repo, {"scripts/focused_job.py": JOB + 'VITEST_ALL = "web/EVERYTHING"\n'})
    _git(repo, "commit", "-qam", "other sentinel")
    with pytest.raises(m.ManifestError, match="base VITEST_ALL"):
        m.Policy(m.Repo(repo), _git(repo, "rev-parse", "HEAD"))


def test_derive_selection_maps_only_vitest_to_the_sentinel():
    whole = {**BOUNDED, "vitest": ["ALL"]}
    assert m.derive_selection(whole)[0] is None  # no VITEST_ALL on the base: refused as before
    assert m.derive_selection(whole, vitest_all="web/ALL")[0]["web"] == ["web/ALL"]
    files = [f"web/src/t{i}.test.ts" for i in range(300)]  # no maximum: many files stay files
    assert m.derive_selection({**BOUNDED, "vitest": files}, vitest_all="web/ALL")[0]["web"] == sorted(files)
    full = {**BOUNDED, "full": True, "pytest": ["ALL"], "vitest": ["ALL"]}
    assert m.derive_selection(full, vitest_all="web/ALL")[0] is None
    assert m.derive_selection({**whole, "pytest": ["ALL"]}, vitest_all="web/ALL")[0] is None


@pytest.mark.parametrize("keys", ["with", "without"])
def test_a_selection_of_300_ids_is_accepted_whether_or_not_the_base_has_id_limits(core, keys):
    """Owner 2026-11-14 (bead hl17, "PRs of any size"): sm reads no MAX_IDS or PYTHON_CI_MAX_IDS from Core and has no
    maximum of its own; a base with or without the keys behaves the same."""
    repo = core["repo"]
    _git(repo, "checkout", "-q", "main")
    if keys == "without":
        job = "\n".join(line for line in JOB.splitlines() if not line.startswith(("MAX_IDS", "PYTHON_CI_MAX_IDS"))) + "\n"
        _write(repo, {"scripts/focused_job.py": job})
        _git(repo, "commit", "-qam", "no id limits")
    policy = m.Policy(m.Repo(repo), _git(repo, "rev-parse", "HEAD"))

    class Blobs:  # check_selection asks the tree for each test file: every one exists here
        def blob(self, tree, path):
            return "0" * 40

    selection = {"python": [f"tests/unit/t{i}_test.py" for i in range(320)]}
    m.check_selection(selection, Blobs(), "tree", policy)  # no refusal
    with pytest.raises(m.ManifestError, match="empty focused lane python"):
        m.check_selection({"python": []}, Blobs(), "tree", policy)
