"""hz-admit-wave: preopen, predict, bind and wave on a scratch Core-shaped repository.

No network, no GitHub, no registry: the real hz-focused-manifest builds the packets, a fake collect_cases.py
lists `def test_*` names of the selected files on the exact tree (plus one case per line of scan.txt, to model a
tree-scanning test), and nothing is prepared or applied.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# (moved: the package is imported, no path insert)
from steuermaschine_heinzel.native.packet_lib import admit_wave as w

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
AFFECTED = '''import json
class Repo:
    def __init__(self, root):
        self.root = root
        self.lanes = {"web-1": [], "web-2": [], "cli": []}
def classify(repo, changed):
    result = json.loads((repo.root / "selector.json").read_text())
    result["files"] = sorted(changed)
    if tests := sorted(p for p in changed if p.startswith("tests/unit/")):  # like Core: a changed test selects itself
        result["pytest"] = tests
    return result
'''
BOUNDED = {"full": False, "pytest": ["tests/unit/a_test.py"], "vitest": [], "e2e_lanes": [], "jobs": ["lint"],
           "reasons": ["1 python source file(s)"]}
COLLECTOR = '''import argparse, json, os, re, subprocess, sys
def cases(repo, tree, sel, lane):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    show = lambda p: subprocess.run(["git", "-C", repo, "show", f"{tree}:{p}"], capture_output=True, text=True, env=env)
    out = []
    for path in json.load(open(sel))[lane]:
        cls = path[:-3].replace("/", ".")
        out += [(cls, n) for n in re.findall(r"def (test_\\w+)", show(path).stdout)]
        scan = show("scan.txt")
        out += [(cls, f"test_scan[{line}]") for line in scan.stdout.split()] if scan.returncode == 0 else []
    return sorted(out)
def write(pairs, path):
    body = "".join(f'<testcase classname="{c}" name="{n}"/>' for c, n in pairs)
    open(path, "w").write(f"<testsuites><testsuite>{body}</testsuite></testsuites>")
def read(path):
    return sorted(re.findall(r'classname="([^"]*)" name="([^"]*)"', open(path).read()))
p = argparse.ArgumentParser(); s = p.add_subparsers(dest="op")
c = s.add_parser("collect"); c.add_argument("--repo"); c.add_argument("--tree"); c.add_argument("--selection")
c.add_argument("--lane"); c.add_argument("--python"); c.add_argument("--node-modules"); c.add_argument("-o")
k = s.add_parser("compare"); k.add_argument("--hosted"); k.add_argument("--pr"); k.add_argument("--result"); k.add_argument("-o")
a = p.parse_args()
if a.op == "collect":
    write(cases(a.repo, a.tree, a.selection, a.lane), a.o); sys.exit(0)
h, pr, res = read(a.hosted), read(a.pr), read(a.result)
if h != pr:
    open(a.o, "w").write("hosted != pr\\n"); sys.exit(1)
open(a.o, "w").write("".join(f"+ {c}::{n}\\n" for c, n in sorted(set(res) - set(pr))))
sys.exit(3 if pr != res else 0)
'''


def _git(repo, *args, date="1700000000 +0000"):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                          env=env).stdout.strip()


def _write(repo, files):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _branch(repo, name, files, start="main"):
    _git(repo, "checkout", "-q", "-b", name, start)
    _write(repo, files)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", name)
    _git(repo, "checkout", "-q", "main")
    return _git(repo, "rev-parse", name)


@pytest.fixture
def wave(tmp_path):
    repo = tmp_path / "core"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "symbolic-ref", "refs/remotes/origin/main", "refs/heads/main")  # a packet base must be on origin/main
    _write(repo, {"scripts/focused_plan.py": PLAN, "scripts/focused_job.py": JOB, "scripts/affected.py": AFFECTED,
                  "scripts/related_tests.py": "", "uv.lock": "lock\n", "pyproject.toml": "[project]\n",
                  "selector.json": json.dumps(BOUNDED), "src/heinzel/mod.py": "x = 1\n", "src/heinzel/other.py": "y = 1\n",
                  "tests/unit/a_test.py": "def test_one(): pass\n", "tests/unit/b_test.py": "def test_b(): pass\n",
                  "docs/readme.md": "doc\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    main = _git(repo, "rev-parse", "HEAD")
    a = _branch(repo, "a", {"src/heinzel/mod.py": "x = 2\n", "tests/unit/a_test.py": "def test_one(): pass\ndef test_two(): pass\n"})
    b = _branch(repo, "b", {"src/heinzel/other.py": "y = 2\n", "tests/unit/b_test.py": "def test_b(): pass\ndef test_c(): pass\n"})
    root = tmp_path / "review"
    root.mkdir()
    collector = tmp_path / "bin/collect_cases.py"
    collector.parent.mkdir()
    collector.write_text(COLLECTOR)
    data = {"repo": repo, "main": main, "a": a, "b": b, "root": root, "collector": collector, "tmp": tmp_path}
    for name, test in (("a", "tests/unit/a_test.py"), ("b", "tests/unit/b_test.py")):
        wt = tmp_path / f"wt-{name}"
        _git(repo, "worktree", "add", "-q", "--detach", str(wt), name)
        sel = tmp_path / f"sel-{name}.json"
        sel.write_text(json.dumps({"python": [test]}))
        junit = tmp_path / f"{name}.junit.xml"
        subprocess.run([sys.executable, str(collector), "collect", "--repo", str(repo), "--tree", data[name],
                        "--selection", str(sel), "--lane", "python", "-o", str(junit)], check=True)
        data[f"wt-{name}"], data[f"sel-{name}"], data[f"junit-{name}"] = wt, sel, junit
    return data


def inputs(d, name, out):
    return [name, "--core-repo", str(d["repo"]), "--worktree", str(d[f"wt-{name}"]), "--out", str(d["root"] / out),
            "--review-root", str(d["root"]), "--selection", str(d[f"sel-{name}"]),
            "--junit", f"python={d[f'junit-{name}']}", "--collector", str(d["collector"]),
            "--members", f"member {name}"]


def run(capsys, *argv):
    code = w.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def outcome(err):
    return [json.loads(line) for line in err.splitlines() if line.startswith("{")][-1]


def preopen(d, capsys, name="a", out=None):
    code, stdout, err = run(capsys, "preopen", *inputs(d, name, out or f"{name}-pre"), "--main", d["main"])
    assert code == 0, err
    return stdout, err


def squash(d, branch, parent, date):
    """What the merge queue does: a new single-parent commit with the merged tree (a different SHA)."""
    tree = _git(d["repo"], "merge-tree", "--write-tree", parent, branch).splitlines()[0]
    return _git(d["repo"], "commit-tree", tree, "-p", parent, "-m", f"squash {branch}", date=date)


def test_help_names_every_subcommand():
    help_text = w.parser().format_help()
    for word in ("preopen", "predict", "bind", "wave", "byte-equal", "Never applies"):
        assert word in help_text
    for sub in ("preopen", "predict", "bind", "wave"):
        with pytest.raises(SystemExit) as done:
            w.main([sub, "--help"])
        assert done.value.code == 0


def test_preopen_builds_a_verified_pair_and_prints_only_a_prepare(wave, capsys):
    stdout, err = preopen(wave, capsys)
    out = wave["root"] / "a-pre"
    packet = json.loads((out / "packet.json").read_text())
    tree = _git(wave["repo"], "merge-tree", "--write-tree", wave["main"], wave["a"]).splitlines()[0]
    assert packet["manifest"]["base"] == wave["main"] and packet["manifest"]["head"] == wave["a"]
    assert packet["manifest"]["tree"] == packet["main_manifest"]["tree"] == tree
    assert packet["review"]["path"] == "a-pre/REVIEW.md"
    record = json.loads((out / "wave.json").read_text())
    assert record["kind"] == "preopen" and record["tree"] == tree
    assert stdout.count("\n") == 1 and " prepare " in stdout and " apply" not in stdout and "--approved" not in stdout
    assert "BYTE-EQUAL" in (out / "verify.err").read_text()
    review = (out / "REVIEW.md").read_text()
    assert "NOT the independent review" in review and "member a" in review
    assert outcome(err) | {} == {"event": "admit_wave.outcome", "operation": "preopen", "result": "success",
                                 "head": wave["a"], "main": wave["main"], "packet_sha256": record["packet_sha256"]}


def test_preopen_records_a_tree_scanning_lane(wave, capsys):
    _write(wave["repo"], {"scan.txt": "one\n"})
    _git(wave["repo"], "add", "scan.txt")
    _git(wave["repo"], "commit", "-qm", "scan")
    main = _git(wave["repo"], "rev-parse", "HEAD")
    code, _, err = run(capsys, "preopen", *inputs(wave, "a", "a-scan"), "--main", main)
    assert code == 0, err
    assert "TREE-SCANNING" in (wave["root"] / "a-scan/collected.txt").read_text()


@pytest.mark.parametrize("case, message", [
    ("short-main", "full 40-hex"),
    ("dirty", "not clean"),
    ("wrong-head", "is not at the head"),
    ("not-empty", "not empty"),
    ("outside", "inside --review-root"),
    ("junit", "does not reproduce the local focused JUnit"),
    ("lanes", "--junit lanes"),
])
def test_preopen_fails_closed(wave, capsys, case, message):
    argv = inputs(wave, "a", "a-bad")
    main = wave["main"]
    if case == "short-main":
        main = main[:12]
    elif case == "dirty":
        (wave["wt-a"] / "src/heinzel/mod.py").write_text("x = 3\n")
    elif case == "wrong-head":
        argv[argv.index(str(wave["wt-a"]))] = str(wave["wt-b"])
    elif case == "not-empty":
        (wave["root"] / "a-bad").mkdir()
        (wave["root"] / "a-bad/x").write_text("")
    elif case == "outside":
        argv[argv.index(str(wave["root"] / "a-bad"))] = str(wave["tmp"] / "elsewhere")
    elif case == "junit":
        wave["junit-a"].write_text('<testsuites><testsuite><testcase classname="tests.unit.a_test" '
                                   'name="test_one"/></testsuite></testsuites>')
    elif case == "lanes":
        argv[argv.index(f"python={wave['junit-a']}")] = f"web={wave['junit-a']}"
    code, stdout, err = run(capsys, "preopen", *argv, "--main", main)
    assert code == 1 and stdout == ""
    assert message in err
    assert outcome(err)["result"] == "refused"
    assert not (wave["root"] / "a-bad/wave.json").exists()


def test_preopen_refuses_a_conflict_with_main(wave, capsys):
    _write(wave["repo"], {"src/heinzel/mod.py": "x = 9\n"})
    _git(wave["repo"], "commit", "-qam", "conflict")
    main = _git(wave["repo"], "rev-parse", "HEAD")
    code, _, err = run(capsys, "preopen", *inputs(wave, "a", "a-c"), "--main", main)
    assert code == 1 and "conflicts with" in err


def test_preopen_keeps_a_reviewed_scope(wave, capsys):
    preopen(wave, capsys)
    _write(wave["repo"], {"docs/readme.md": "doc 2\n"})
    _git(wave["repo"], "commit", "-qam", "docs")
    main = _git(wave["repo"], "rev-parse", "HEAD")
    argv = inputs(wave, "a", "a-next")
    argv[argv.index("--selection"):argv.index("--selection") + 2] = ["--reviewed-packet", str(wave["root"] / "a-pre/packet.json")]
    code, _, err = run(capsys, "preopen", *argv, "--main", main)
    assert code == 0, err
    new = json.loads((wave["root"] / "a-next/packet.json").read_text())
    assert new["manifest"]["base"] == main


def test_inherited_git_dir_cannot_redirect_reads(wave, capsys, monkeypatch, tmp_path):
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git/index"))
    preopen(wave, capsys)
    assert not list((decoy / ".git/refs").rglob("sm-wave*"))


def predict(wave, capsys, out="b-pred", after=("a-pre",)):
    argv = ["predict", *inputs(wave, "b", out)]
    for name in after:
        argv += ["--after", str(wave["root"] / name)]
    return run(capsys, *argv)


def test_predict_binds_the_predicted_main_and_prints_no_prepare(wave, capsys):
    preopen(wave, capsys)
    code, stdout, err = predict(wave, capsys)
    assert code == 0, err
    assert stdout == ""
    record = json.loads((wave["root"] / "b-pred/wave.json").read_text())
    a_tree = json.loads((wave["root"] / "a-pre/wave.json").read_text())["tree"]
    sim = record["predicted_main"]
    assert record["main"] == sim and record["parent_main"] == wave["main"] and record["predicted_main_tree"] == a_tree
    assert _git(wave["repo"], "rev-parse", f"{sim}^{{tree}}") == a_tree
    assert _git(wave["repo"], "rev-parse", f"{sim}^") == wave["main"]
    assert _git(wave["repo"], "rev-parse", f"refs/sm-wave/predicted/{sim}") == sim
    assert json.loads((wave["root"] / "b-pred/packet.json").read_text())["manifest"]["base"] == sim
    assert "PREDICTED base" in (wave["root"] / "b-pred/REVIEW.md").read_text()
    assert _git(wave["repo"], "rev-parse", "main") == wave["main"]  # no branch moved


def test_predict_refuses_a_predict_dir_as_earlier_head(wave, capsys):
    preopen(wave, capsys)
    assert predict(wave, capsys)[0] == 0
    code, _, err = predict(wave, capsys, out="b-again", after=("b-pred",))
    assert code == 1 and "preopen or bound" in err


def test_predict_refuses_a_tampered_earlier_packet(wave, capsys):
    preopen(wave, capsys)
    path = wave["root"] / "a-pre/packet.json"
    path.write_text(path.read_text() + " ")
    code, _, err = predict(wave, capsys)
    assert code == 1 and "changed since it was built" in err


def test_bind_rebinds_only_the_base_when_the_trees_are_byte_equal(wave, capsys):
    preopen(wave, capsys)
    assert predict(wave, capsys)[0] == 0
    actual = squash(wave, "a", wave["main"], "1700000500 +0000")
    record = json.loads((wave["root"] / "b-pred/wave.json").read_text())
    assert actual != record["predicted_main"]
    code, stdout, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", actual)
    assert code == 0, err
    out = wave["root"] / f"b-pred/bound-{actual[:12]}"
    old = json.loads((wave["root"] / "b-pred/packet.json").read_text())
    new = json.loads((out / "packet.json").read_text())
    for part in ("manifest", "main_manifest"):
        assert new[part]["base"] == actual
        assert {k: v for k, v in new[part].items() if k != "base"} == {k: v for k, v in old[part].items() if k != "base"}
    bound = json.loads((out / "wave.json").read_text())
    assert bound["kind"] == "bound" and bound["main"] == actual
    assert f"prepare {out / 'packet.json'}" in stdout and "apply" not in stdout
    assert "Bind to real main" in (out / "REVIEW.md").read_text()


@pytest.mark.parametrize("case, message", [
    ("extra-change", "!= predicted main tree"),
    ("two-commits", "another commit merged"),
    ("unrelated", "does not descend"),
    ("merge-commit", "not a single-parent squash"),
])
def test_bind_refuses_and_says_reprepare(wave, capsys, case, message):
    preopen(wave, capsys)
    assert predict(wave, capsys)[0] == 0
    repo, main = wave["repo"], wave["main"]
    if case == "extra-change":
        _branch(repo, "a2", {"docs/readme.md": "changed while in CI\n"}, start="a")
        actual = squash(wave, "a2", main, "1700000600 +0000")
    elif case == "two-commits":
        docs = _git(repo, "commit-tree", _git(repo, "rev-parse", f"{main}^{{tree}}"), "-p", main, "-m", "noop")
        actual = squash(wave, "a", docs, "1700000700 +0000")
    elif case == "unrelated":
        actual = _git(repo, "commit-tree", _git(repo, "rev-parse", f"{main}^{{tree}}"), "-m", "root")
    else:
        tree = _git(repo, "merge-tree", "--write-tree", main, "a").splitlines()[0]
        actual = _git(repo, "commit-tree", tree, "-p", main, "-p", wave["a"], "-m", "merge")
    code, stdout, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", actual)
    assert code == 1 and stdout == ""
    assert message in err and "re-prepare" in err
    assert not (wave["root"] / f"b-pred/bound-{actual[:12]}").exists()


def test_bind_refuses_a_preopen_dir_and_a_short_sha(wave, capsys):
    preopen(wave, capsys)
    code, _, err = run(capsys, "bind", str(wave["root"] / "a-pre"), "--main", wave["main"])
    assert code == 1 and "not a predict dir" in err
    assert predict(wave, capsys)[0] == 0
    code, _, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", "main")
    assert code == 1 and "full 40-hex" in err


def test_wave_prints_one_batch_prepare_for_one_main(wave, capsys):
    preopen(wave, capsys)
    preopen(wave, capsys, name="b")
    code, stdout, err = run(capsys, "wave", str(wave["root"] / "a-pre"), str(wave["root"] / "b-pre"))
    assert code == 0, err
    assert stdout.count("\n") == 1 and "prepare --packets" in stdout and "--core-repos" in stdout
    assert stdout.index("a-pre/packet.json") < stdout.index("b-pre/packet.json")
    assert "apply" not in stdout


def test_wave_refuses_mixed_mains_single_dirs_and_predict_dirs(wave, capsys):
    preopen(wave, capsys)
    code, _, err = run(capsys, "wave", str(wave["root"] / "a-pre"))
    assert code == 1 and "2..20" in err
    assert predict(wave, capsys)[0] == 0
    code, _, err = run(capsys, "wave", str(wave["root"] / "a-pre"), str(wave["root"] / "b-pred"))
    assert code == 1 and "bind them first" in err
    _write(wave["repo"], {"docs/readme.md": "doc 3\n"})
    _git(wave["repo"], "commit", "-qam", "docs")
    main = _git(wave["repo"], "rev-parse", "HEAD")
    code, _, err = run(capsys, "preopen", *inputs(wave, "b", "b-other"), "--main", main)
    assert code == 0, err
    code, _, err = run(capsys, "wave", str(wave["root"] / "a-pre"), str(wave["root"] / "b-other"))
    assert code == 1 and "different mains" in err



def test_bind_and_preopen_refuse_a_predicted_main(wave, capsys):
    preopen(wave, capsys)
    assert predict(wave, capsys)[0] == 0
    sim = json.loads((wave["root"] / "b-pred/wave.json").read_text())["predicted_main"]
    code, _, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", sim)
    assert code == 1 and "predicted commit" in err
    code, _, err = run(capsys, "preopen", *inputs(wave, "a", "a-sim"), "--main", sim)
    assert code == 1 and "predicted commit" in err
    on_top = _git(wave["repo"], "commit-tree", _git(wave["repo"], "rev-parse", f"{sim}^{{tree}}"), "-p", sim, "-m", "x")
    code, _, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", on_top)
    assert code == 1 and "descends from a predicted commit" in err
    # the real main the prediction started from stays usable for other heads
    code, _, err = run(capsys, "preopen", *inputs(wave, "b", "b-pre"), "--main", wave["main"])
    assert code == 0, err


@pytest.mark.parametrize("child", ["failure", "error", "skipped"])
def test_preopen_refuses_a_local_junit_that_is_not_green(wave, capsys, child):
    text = wave["junit-a"].read_text()
    wave["junit-a"].write_text(text.replace('name="test_two"/>', f'name="test_two"><{child}/></testcase>'))
    code, _, err = run(capsys, "preopen", *inputs(wave, "a", "a-red"), "--main", wave["main"])
    assert code == 1 and "not a green run" in err


def test_refused_bind_leaves_no_review_beside_a_packet(wave, capsys, monkeypatch):
    preopen(wave, capsys)
    assert predict(wave, capsys)[0] == 0
    actual = squash(wave, "a", wave["main"], "1700000500 +0000")
    real = w.manifest

    def drifting(*args, **kwargs):
        packet = real(*args, **kwargs)
        packet["manifest"]["mode"] = "drifted"
        return packet
    monkeypatch.setattr(w, "manifest", drifting)
    code, _, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", actual)
    assert code == 1 and "differs from the prediction in ['mode']" in err
    out = wave["root"] / f"b-pred/bound-{actual[:12]}"
    assert not (out / "REVIEW.md").exists() and not (out / "packet.json").exists() and not (out / "wave.json").exists()


def test_bind_refuses_a_changed_collector(wave, capsys):
    preopen(wave, capsys)
    assert predict(wave, capsys)[0] == 0
    record = json.loads((wave["root"] / "b-pred/wave.json").read_text())
    assert record["collector_sha256"] and record["python_version"]
    wave["collector"].write_text(wave["collector"].read_text() + "\n# changed\n")
    actual = squash(wave, "a", wave["main"], "1700000500 +0000")
    code, _, err = run(capsys, "bind", str(wave["root"] / "b-pred"), "--main", actual)
    assert code == 1 and "collector or the collection python changed" in err


def test_predict_refuses_the_shared_core_repository(wave, capsys, monkeypatch):
    preopen(wave, capsys)
    monkeypatch.setattr(w, "LIVE_CORE", wave["repo"])
    code, _, err = predict(wave, capsys)
    assert code == 1 and "scratch clone" in err
    assert not _git(wave["repo"], "for-each-ref", w.PREDICTED_REFS)


def test_printed_commands_are_shell_quoted(wave, capsys, tmp_path):
    preopen(wave, capsys)
    preopen(wave, capsys, name="b")
    odd = tmp_path / "stuck receipts.json"
    odd.write_text("{}")
    code, stdout, _ = run(capsys, "wave", str(wave["root"] / "a-pre"), str(wave["root"] / "b-pre"), "--nonconsumers", str(odd))
    assert code == 0
    import shlex
    argv = shlex.split(stdout)
    assert argv[argv.index("--nonconsumers") + 1] == str(odd.resolve())


def test_preopen_reads_an_empty_lane_list_as_no_lane(wave, capsys):
    """igag: a selection with an empty list (e.g. "python": [] on a web-only head) means that lane is not selected."""
    wave["sel-a"].write_text(json.dumps({"python": ["tests/unit/a_test.py"], "web": [], "vitest": []}))
    stdout, _ = preopen(wave, capsys, out="a-empty")
    assert json.loads((wave["root"] / "a-empty/sel.json").read_text()) == {"python": ["tests/unit/a_test.py"]}
    assert " prepare " in stdout
    wave["sel-a"].write_text(json.dumps({"python": []}))
    code, _, err = run(capsys, "preopen", *inputs(wave, "a", "a-none"), "--main", wave["main"])
    assert code == 1 and "non-empty" in err
