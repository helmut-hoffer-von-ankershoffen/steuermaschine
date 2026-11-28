"""queue_lib.main_checkpoint_reader: the bounded command runner, verified_segment and main on a fake protected source
(a tar of tiny `scripts` modules) with every git and gh call answered by a scripted `command`."""

import io
import json
import os
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.queue_lib import main_checkpoint_reader as rd

SHA = "a" * 40
BASE = "b" * 40
READER = "c" * 40

PLAN = '''
import json
MAX_PLAN = 100
def require(ok, message):
    if not ok:
        raise ValueError(message)
def valid_sha(v):
    return isinstance(v, str) and len(v) == 40
def unique_object(pairs):
    return dict(pairs)
def object_id(name):
    return "tree-of-" + name
def raw_diff(base, head):
    return "diff"
class WorkloadSource:
    def __init__(self, *a):
        self.args = a
class Scope:
    tree = "T"
    manifest_digest = "D"
def resolve(source, head, selected):
    return Scope() if "ok" in selected else "refused"
HAS_REGISTRY
'''

GATE = '''
RUNS = {}
CALLS = []
def git(*args):
    CALLS.append(args)
    if args[0] == "merge-base" and args[1] == "--is-ancestor":
        return b""
    return b"BASE_OUT"
def _authenticated_run(gate, run_id):
    return RUNS.get("run")
def _verify_config(scope):
    CALLS.append("config")
def _verify_workload(gate, source, scope, read):
    CALLS.append("workload")
class GateSource:
    def __init__(self, *a):
        self.args = a
class Gate:
    def __init__(self, repo, token, head):
        self.repo = repo
'''


def modules(registry=True):
    plan = PLAN.replace("HAS_REGISTRY", "def plan_registry(legacy, slots):\n    return {'legacy': legacy, 'slots': list(slots)}\n" if registry else "")
    return {"scripts/focused_plan.py": plan, "scripts/focused_gate.py": GATE, "scripts/__init__.py": ""}


def archive(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as bundle:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            bundle.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


def test_command_returns_bounded_output_and_refuses_failures(monkeypatch):
    assert rd.command([sys.executable, "-c", "print('hi')"]) == b"hi\n"
    with pytest.raises(ValueError, match="failed or exceeded"):
        rd.command([sys.executable, "-c", "import sys; sys.exit(3)"])
    with pytest.raises(ValueError, match="failed or exceeded"):
        rd.command([sys.executable, "-c", "print('x' * 100)"], limit=10)


def test_api_and_read_call_gh(monkeypatch):
    seen = []
    monkeypatch.setattr(rd, "command", lambda args, **kw: seen.append((args, kw)) or b'{"a": 1}')
    assert rd.api("git/ref") == {"a": 1} and rd.read("contents/x") == b'{"a": 1}'
    assert seen[0][0] == ["gh", "api", f"repos/{rd.REPOSITORY}/git/ref"] and seen[1][1] == {"limit": 5 * 1024 * 1024}


class Fp:
    def __init__(self, valid=True, resolved=True, registry=None, tree_key=False):
        self.valid, self.resolved, self.tree_key = valid, resolved, tree_key

    MAX_PLAN = 100
    Scope = type("Scope", (), {"tree": "T", "manifest_digest": "D"})

    def require(self, ok, message):
        if not ok:
            raise ValueError(message)

    def valid_sha(self, value):
        return self.valid and isinstance(value, str) and len(value) == 40

    def object_id(self, name):
        return "tree1"

    WorkloadSource = lambda self, *a: ("source", a)  # noqa: E731

    def raw_diff(self, base, head):
        return "d"

    def resolve(self, source, head, selected):
        return self.Scope() if self.resolved else "no"


class Fg:
    def __init__(self, run=None):
        self.run = run or {"run_attempt": 1, "event": "push", "head_branch": "main", "head_repository": {"full_name": rd.REPOSITORY}}
        self.calls = []

    class Gate:
        def __init__(self, repo, token, head):
            self.repo = repo

    GateSource = lambda self, *a: ("gs", a)  # noqa: E731

    def git(self, *args):
        self.calls.append(args)
        return BASE.encode() + b"\n" if args[0] == "merge-base" and args[1] != "--is-ancestor" else b""

    def _authenticated_run(self, gate, run_id):
        return self.run

    def _verify_config(self, scope):
        self.calls.append("config")

    def _verify_workload(self, gate, source, scope, reader):
        self.calls.append("workload")


def request(**more):
    return {"head": SHA, "base": BASE, "reader_head": READER, "run": 7, "registry": {f"push:{SHA}": {"ok": 1}}, **more}


def segment(monkeypatch, req=None, fp=None, fg=None, main_sha=READER, later_sha=None):
    answers = iter([{"object": {"sha": main_sha}}, {"object": {"sha": later_sha or main_sha}}])
    monkeypatch.setattr(rd, "api", lambda path: next(answers))
    fg = fg or Fg()
    fg.Gate = type("Gate", (), {"__init__": lambda self, repo, token, head: None})
    return rd.verified_segment(req or request(), fp or Fp(), fg)


def test_verified_segment_returns_the_manifest(monkeypatch):
    out = segment(monkeypatch)
    assert out == {"type": "focused-main/v1", "base": BASE, "head": SHA, "tree": "T", "run": 7, "attempt": 1, "manifest_sha256": "D"}
    # the tree manifest key is the fallback to the push key
    out = segment(monkeypatch, req=request(registry={"push-tree:tree1": {"ok": 1}}))
    assert out["run"] == 7


@pytest.mark.parametrize("name,kwargs,message", [
    ("invalid sha", dict(fp=Fp(valid=False)), "invalid focused interval"),
    ("main moved", dict(main_sha="d" * 40), "protected main advanced"),
    ("not original", dict(fg=Fg(run={"run_attempt": 2, "event": "push", "head_branch": "main", "head_repository": {"full_name": rd.REPOSITORY}})), "not original"),
    ("wrong repo", dict(fg=Fg(run={"run_attempt": 1, "event": "push", "head_branch": "main", "head_repository": {"full_name": "x/y"}})), "not original"),
    ("no manifest", dict(req=request(registry={})), "missing exact main manifest"),
    ("unresolved", dict(fp=Fp(resolved=False)), "cannot substitute"),
    ("main drifted", dict(later_sha="e" * 40), "main or historical workflow changed"),
])
def test_verified_segment_refusals(monkeypatch, name, kwargs, message):
    with pytest.raises(ValueError, match=message):
        segment(monkeypatch, **kwargs)


def test_verified_segment_refuses_a_base_that_is_not_an_ancestor(monkeypatch):
    fg = Fg()
    fg.git = lambda *args: b"f" * 40 + b"\n" if args[1] != "--is-ancestor" else b""
    with pytest.raises(ValueError, match="base is not an ancestor"):
        segment(monkeypatch, fg=fg)


def test_the_evidence_gate_reads_through_api(monkeypatch):
    seen = []
    fg = Fg()

    class Base:
        def __init__(self, repo, token, head):
            pass

    fg.Gate = Base
    monkeypatch.setattr(rd, "api", lambda path: seen.append(path) or {"object": {"sha": READER}})
    fg._authenticated_run = lambda gate, run_id: (seen.append(gate.github("actions/runs/7")) or fg.run)
    rd.verified_segment(request(), Fp(), fg)
    assert any(isinstance(item, dict) for item in seen)


def run_main(monkeypatch, tmp_path, req, files, registry_result=None, scripted=None):
    """main() with stdin, git/gh and the source archive faked; returns what main printed."""
    raw = json.dumps(req).encode()
    monkeypatch.setattr(rd.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    env = dict(os.environ)
    monkeypatch.setattr(rd.os, "environ", env)
    monkeypatch.setattr(rd.sys, "path", list(sys.path))
    for name in ("scripts.focused_plan", "scripts.focused_gate", "scripts"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    seen = []

    def command(args, **kw):
        seen.append(args)
        if args[:2] == ["git", "rev-parse"] and args[2] == "--local-env-vars":
            return b"GIT_DIR\nGIT_INDEX_FILE\n"
        if args[:2] == ["git", "rev-parse"]:
            return (scripted or READER).encode() + b"\n"
        if args[:2] == ["git", "clone"]:
            Path(args[-1]).mkdir()
        if args[:2] == ["git", "archive"]:
            return archive(files)
        return b""

    monkeypatch.setattr(rd, "command", command)
    monkeypatch.chdir(tmp_path)
    env["GIT_DIR"] = "x"
    rd.main()
    return seen, env


def test_main_registry_mode_with_and_without_plan_registry(monkeypatch, tmp_path, capsys):
    req = {"reader_head": READER, "core_repo": str(tmp_path), "mode": "registry", "legacy": "{}", "slots": ["", ""]}
    seen, env = run_main(monkeypatch, tmp_path, req, modules(True))
    assert json.loads(capsys.readouterr().out) == {"legacy": "{}", "slots": ["", ""]}
    assert "GIT_DIR" not in env and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert ["git", "clone", "--shared", "--no-checkout", "--", str(tmp_path), str(next(a for a in seen if a[:2] == ["git", "clone"])[-1])] in seen


def test_main_registry_fallback_reader_paths(monkeypatch, tmp_path, capsys):
    base = {"reader_head": READER, "core_repo": str(tmp_path), "mode": "registry", "slots": ["", ""]}
    run_main(monkeypatch, tmp_path, {**base, "legacy": '{"a": 1}'}, modules(False))
    assert json.loads(capsys.readouterr().out) == {"a": 1}
    run_main(monkeypatch, tmp_path, {**base, "legacy": ""}, modules(False))
    assert json.loads(capsys.readouterr().out) == {}
    with pytest.raises(ValueError, match="does not support compressed slots"):
        run_main(monkeypatch, tmp_path, {**base, "legacy": "{}", "slots": ["x"]}, modules(False))
    with pytest.raises(ValueError, match="exceeds bound"):
        run_main(monkeypatch, tmp_path, {**base, "legacy": "x" * 500}, modules(False))
    with pytest.raises(ValueError, match="invalid focused registry"):
        run_main(monkeypatch, tmp_path, {**base, "legacy": "[1]"}, modules(False))


def test_main_refuses_a_different_main_and_bad_requests(monkeypatch, tmp_path):
    req = {"reader_head": READER, "core_repo": str(tmp_path), "mode": "registry", "legacy": "{}", "slots": []}
    with pytest.raises(ValueError, match="differs from requested source"):
        run_main(monkeypatch, tmp_path, req, modules(True), scripted="d" * 40)
    for bad in (5, "short", "Z" * 40):
        with pytest.raises(ValueError, match="invalid authenticated source SHA"):
            run_main(monkeypatch, tmp_path, {**req, "reader_head": bad}, modules(True))
    monkeypatch.setattr(rd.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"x" * (rd.MAX_INPUT + 1))))
    with pytest.raises(ValueError, match="exceeds bound"):
        rd.main()


TREE = "d" * 40


class FullGate:
    WORKFLOW = ".github/workflows/ci.yml"

    class Gate:
        def __init__(self, repo, pr, head):
            self.repo, self.pr, self.head = repo, pr, head

    def __init__(self):
        self.verdict = "Whole-project coverage measured by full PR CI"
        self.merge_tree = TREE
        self.current = READER
        self.proof = {"type": "full-pr-capacity/v1", "head": SHA, "base": READER, "tree": TREE,
                      "run": 7, "attempt": 2, "original_attempt": 1}

    def git(self, *args):
        assert args == ("show", f"{READER}:{self.WORKFLOW}")
        return b"trusted workflow"

    def full_pr_jobs(self):
        pass

    def verify(self, gate, run, manifest, read):
        assert (gate.repo, gate.pr, gate.head, run, manifest) == (rd.REPOSITORY, "42", SHA, 7, "{}")
        assert gate.github("identity") == {"checked": True}
        assert read("artifact") == b"receipt"
        return self.verdict

    def verify_full_pr_capacity(self, gate, run, read):
        assert (gate.pr, run, read("artifact")) == ("42", 7, b"receipt")
        return self.proof

    def pull_identity(self, gate, head):
        assert head == SHA
        return {}, "merge-sha", self.current


def full_request(mode="full-pr"):
    return {"mode": mode, "head": SHA, "reader_head": READER, "tree": TREE, "pr": 42, "run": 7}


def full_reader(monkeypatch, tmp_path, request=None, gate=None, fp=None):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rd, "api", lambda path: {"checked": True})
    monkeypatch.setattr(rd, "read", lambda path: b"receipt")
    fp = fp or Fp()
    fp.object_id = lambda name: TREE if name == "merge-sha^{tree}" else None
    return rd.verified_full_pr(request or full_request(), fp, gate or FullGate())


@pytest.mark.parametrize("mode", ["full-pr", "full-pr-capacity"])
def test_full_reader_authenticates_workflow_and_exact_proof(monkeypatch, tmp_path, mode):
    result = full_reader(monkeypatch, tmp_path, full_request(mode))
    assert result == {"type": f"{mode}/v1", "head": SHA, "base": READER, "tree": TREE,
                      "run": 7, "attempt": 2 if mode.endswith("capacity") else 1,
                      **({"original_attempt": 1} if mode.endswith("capacity") else {})}
    assert (tmp_path / FullGate.WORKFLOW).read_bytes() == b"trusted workflow"


@pytest.mark.parametrize("failure", ["unsupported", "sha", "verdict", "base", "tree"])
def test_full_reader_refuses_unverified_source_or_identity(monkeypatch, tmp_path, failure):
    gate = FullGate()
    req = full_request()
    if failure == "unsupported":
        gate.full_pr_jobs = None
    elif failure == "sha":
        req["tree"] = "bad"
    elif failure == "verdict":
        gate.verdict = "focused evidence only"
    elif failure == "base":
        gate.current = "e" * 40
    else:
        req["tree"] = "e" * 40
    with pytest.raises(ValueError):
        full_reader(monkeypatch, tmp_path, req, gate)


@pytest.mark.parametrize("change", [{"run": 8}, {"attempt": 1}, {"original_attempt": 2}, {"run": True},
                                    {"base": "e" * 40}, {"tree": "e" * 40}])
def test_full_reader_rejects_contradictory_capacity_proofs(monkeypatch, tmp_path, change):
    gate = FullGate()
    gate.proof.update(change)
    with pytest.raises(ValueError, match="not authenticated"):
        full_reader(monkeypatch, tmp_path, full_request("full-pr-capacity"), gate)


@pytest.mark.parametrize(("mode", "selected"), [("full-pr", "full"), ("full-pr-capacity", "full"), ("segment", "segment")])
def test_main_dispatches_only_to_selected_protected_reader(monkeypatch, tmp_path, capsys, mode, selected):
    calls = []
    monkeypatch.setattr(rd, "verified_full_pr", lambda request, fp, fg: calls.append(("full", request["mode"])) or {"reader": "full"})
    monkeypatch.setattr(rd, "verified_segment", lambda request, fp, fg: calls.append(("segment", request["mode"])) or {"reader": "segment"})
    run_main(monkeypatch, tmp_path, {"reader_head": READER, "core_repo": str(tmp_path), "mode": mode}, modules())
    assert calls == [(selected, mode)]
    assert json.loads(capsys.readouterr().out) == {"reader": selected}
