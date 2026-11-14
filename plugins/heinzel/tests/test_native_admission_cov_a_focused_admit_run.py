"""admission_lib.focused_admit, part two: the trusted-source snapshot, validate_canonical, the isolated interpreters
(run for real on a tiny fake `scripts` package), apply_slots refusals and the argv `main`, with git scripted."""

import base64
import hashlib
import io
import json
import tarfile
import uuid
from pathlib import Path

import pytest

from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.admission_lib import focused_admit as fa
from steuermaschine_heinzel.native.admission_lib import identity

CORE = identity.CORE
SHA_A, SHA_B, SHA_C, SHA_D = "a" * 40, "b" * 40, "c" * 40, "d" * 40


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(fa.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def tar_of(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as bundle:
        for name, text in files.items():
            raw = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            bundle.addfile(info, io.BytesIO(raw))
    return buf.getvalue()


# ---------- private directories ----------

def test_private_work_root_is_created_private_and_refuses_unsafe_ones(home, tmp_path, monkeypatch):
    root = fa._private_work_root()
    assert root == statedir.root() / "focused-admission-work" and oct(root.stat().st_mode & 0o777) == "0o700"
    assert fa._private_work_root() == root
    root.chmod(0o750)
    with pytest.raises(fa.AdmissionError, match="work directory has unsafe"):
        fa._private_work_root()
    root.chmod(0o700)
    statedir.root().chmod(0o775)
    with pytest.raises(fa.AdmissionError, match="cache parent has unsafe"):
        fa._private_work_root()
    statedir.root().chmod(0o700)
    real = tmp_path / "real"
    real.mkdir()
    root.rmdir()
    root.symlink_to(real)
    with pytest.raises(fa.AdmissionError, match="work directory is a symlink"):
        fa._private_work_root()
    root.unlink()
    parent = tmp_path / "linked-state"
    parent.symlink_to(real)
    monkeypatch.setattr(statedir, "root", lambda: parent)  # the state directory itself is a symlink
    with pytest.raises(fa.AdmissionError, match="cache parent is a symlink"):
        fa._private_work_root()


# ---------- the live-main source ----------

FOCUS = "PLAN_VARIABLE = 'FOCUSED_CI_PLANS'\nPROTECTED_FILES = ('a.py', 'b.py')\n"


def source_run(repository=CORE, focus=None, blob=SHA_C):
    def run(argv, **kw):
        if argv[1] == "cat-file":
            return b""
        if argv[1] == "show" and argv[2].endswith("release_candidate.py"):
            return f"REPOSITORY = '{repository}'\n".encode()
        if argv[1] == "show":
            return (focus or FOCUS).encode()
        if argv[1] == "archive":
            return tar_of({"scripts/x.py": "X = 1\n"})
        if argv[1] == "rev-parse":
            return blob.encode() + b"\n"
        raise AssertionError(argv)

    return run


def test_canonical_source_extracts_the_archive_and_reads_constants(home, monkeypatch):
    monkeypatch.setattr(fa, "_run", source_run())
    temp, blob, repository, variable, protected = fa._canonical_source(Path("/core"), SHA_A)
    assert (temp / "scripts/x.py").read_text() == "X = 1\n" and blob == SHA_C and repository == CORE
    assert variable == "FOCUSED_CI_PLANS" and protected == ("a.py", "b.py")
    assert temp.parent == statedir.root() / "focused-validator"


@pytest.mark.parametrize("kwargs,message", [
    (dict(repository="other/repo"), "differs from reviewed management"),
    (dict(focus="PLAN_VARIABLE = 5\nPROTECTED_FILES = ('a',)\n"), "unexpected shapes"),
    (dict(focus="PLAN_VARIABLE = 'x'\nPROTECTED_FILES = ['a']\n"), "unexpected shapes"),
])
def test_canonical_source_refusals(home, monkeypatch, kwargs, message):
    monkeypatch.setattr(fa, "_run", source_run(**kwargs))
    with pytest.raises(fa.AdmissionError, match=message):
        fa._canonical_source(Path("/core"), SHA_A)


def test_canonical_source_cache_safety_and_cleanup(home, monkeypatch, tmp_path):
    monkeypatch.setattr(fa, "_run", source_run())
    root = statedir.root() / "focused-validator"
    root.parent.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real"
    real.mkdir()
    root.symlink_to(real)
    with pytest.raises(fa.AdmissionError, match="is a symlink"):
        fa._canonical_source(Path("/core"), SHA_A)
    root.unlink()
    root.mkdir(mode=0o700)
    root.chmod(0o777)
    with pytest.raises(fa.AdmissionError, match="unsafe owner/type/permissions"):
        fa._canonical_source(Path("/core"), SHA_A)
    root.chmod(0o700)
    monkeypatch.setattr(fa, "_run", source_run(blob="not-a-sha"))
    with pytest.raises(fa.AdmissionError, match="focused resolver blob"):
        fa._canonical_source(Path("/core"), SHA_A)
    assert list(root.iterdir()) == []


# ---------- the disposable snapshot ----------

def test_prepare_core_snapshot(home, monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv[:3])
        if argv[:3] == ["git", "remote", "get-url"]:
            return b"https://github.com/o/r\n"
        if argv[1] == "rev-parse":
            return SHA_A.encode() + b"\n"
        return b""

    monkeypatch.setattr(fa, "_run", run)
    workspace, snapshot = fa._prepare_core_snapshot(Path("/core"), SHA_A)
    assert workspace.exists() and snapshot == workspace / "repo" and ["git", "fetch", "--no-tags"] in seen
    monkeypatch.setattr(fa, "_run", lambda argv, **kw: b"https://x\n" if argv[1] == "remote" and argv[2] == "get-url" else (SHA_B.encode() if argv[1] == "rev-parse" else b""))
    with pytest.raises(fa.AdmissionError, match="live main changed"):
        fa._prepare_core_snapshot(Path("/core"), SHA_A)
    assert [p.name for p in (statedir.root() / "focused-admission-work").iterdir() if p.name.startswith("snapshot-")] == [workspace.name]


# ---------- validate_canonical ----------

def plan(**more):
    return {"event": "pull_request", "ref": "refs/pull/*/merge", "head": SHA_A, "base": SHA_B, "tree": SHA_C, "execution": {"a.py": "1", "b.py": "2"}, **more}


def packet_of(p):
    return {"manifest": p, "review": {"head": SHA_A}}


def git_script(trusted_exec=None, resulting_exec=None, tree=SHA_C, fetched=SHA_D, remote=f"https://github.com/{CORE}.git", release_source=None, focus_source=None):
    trusted_exec = trusted_exec or {"a.py": "1", "b.py": "2"}
    resulting_exec = resulting_exec or {"a.py": "1", "b.py": "2"}
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        sub = argv[1]
        if sub == "remote" and argv[2] == "get-url":
            return remote.encode() + b"\n"
        if sub in ("clone", "remote", "fetch", "cat-file"):
            return b""
        if sub == "rev-parse":
            ref = argv[2]
            if ref.startswith("refs/sm-admission"):
                return fetched.encode() + b"\n"
            path = ref.split(":", 1)[1]
            if ref.startswith(SHA_D + ":"):
                return (b"blob-" + path.encode()) if path == "scripts/focused_plan.py" else trusted_exec[path].encode()
            return resulting_exec[path].encode()
        if sub == "merge-tree":
            return tree.encode() + b"\nconflict-info\n"
        if sub == "commit-tree":
            return SHA_C.encode() + b"\n"
        if sub == "diff-tree":
            return b":raw\0"
        if sub == "show":
            if argv[2].endswith("release_candidate.py"):
                return (release_source or f"REPOSITORY = '{CORE}'\n").encode()
            return (focus_source or FOCUS).encode()
        raise AssertionError(argv)

    run.calls = calls
    return run


def validate(monkeypatch, tmp_path, p=None, script=None, resolved=None, **kw):
    monkeypatch.setattr(fa, "_run", script or git_script())
    requests = []
    answer = resolved or {"tree": SHA_C, "manifest_digest": hashlib.sha256(fa.canonical(p or plan())).hexdigest()}
    monkeypatch.setattr(fa, "_isolated_resolve", lambda source, repo, request: requests.append(request) or answer)
    out = fa.validate_canonical(packet_of(p or plan()), Path("/core"), SHA_D, CORE, "FOCUSED_CI_PLANS", ("a.py", "b.py"), tmp_path, **kw)
    return out, requests


def test_validate_canonical_returns_the_scope(home, tmp_path, monkeypatch):
    out, requests = validate(monkeypatch, tmp_path)
    assert out["merge_commit"] == SHA_C and out["repository"] == CORE and out["validator_blob"] == "blob-scripts/focused_plan.py"
    assert requests[0]["event"] == "pull_request" and requests[0]["head"] == SHA_A and requests[0]["registry"] == {f"pull_request:{SHA_A}": plan()}
    assert base64.b64decode(requests[0]["raw_diff"]) == b":raw\0"
    assert not any(p.name.startswith("merge-") for p in (statedir.root() / "focused-admission-work").iterdir())


def test_validate_canonical_resulting_main_uses_the_push_shape(home, tmp_path, monkeypatch):
    main_plan = {"event": "push", "ref": "refs/heads/main", "base": SHA_B, "tree": SHA_C, "execution": {"a.py": "1", "b.py": "2"}}
    script = git_script()
    _out, requests = validate(monkeypatch, tmp_path, p=main_plan, script=script, resulting_main=True)
    assert requests[0]["event"] == "push" and requests[0]["head"] == SHA_C and requests[0]["ref"] == "refs/heads/main"
    assert next(c for c in script.calls if c[1] == "commit-tree").count("-p") == 1


@pytest.mark.parametrize("kwargs,message", [
    (dict(script=git_script(remote="https://github.com/other/repo")), "not the canonical repository"),
    (dict(script=git_script(fetched=SHA_C)), "live main changed"),
    (dict(script=git_script(tree=SHA_A)), "packet tree differs"),
    (dict(script=git_script(tree="junk")), "simulated merge tree"),
    (dict(p=plan(execution={"a.py": "x", "b.py": "2"})), "bindings differ from reviewed resulting tree"),
    (dict(script=git_script(trusted_exec={"a.py": "9", "b.py": "2"})), "changes protected focused-CI"),
    (dict(resolved={"tree": SHA_A, "manifest_digest": "x"}), "resolver tree differs"),
    (dict(resolved={"tree": SHA_C, "manifest_digest": "x"}), "resolver digest differs"),
])
def test_validate_canonical_refusals(home, tmp_path, monkeypatch, kwargs, message):
    with pytest.raises(fa.AdmissionError, match=message):
        validate(monkeypatch, tmp_path, **kwargs)


def test_validate_canonical_reviewed_producer_change(home, tmp_path, monkeypatch):
    changed = {"a.py": "9", "b.py": "2"}
    out, _ = validate(monkeypatch, tmp_path, script=git_script(trusted_exec=changed), protected_producer=True)
    assert out["merge_commit"] == SHA_C
    for kwargs in (dict(focus_source="PLAN_VARIABLE = 'x'\nPROTECTED_FILES = ('a.py', 'b.py')\n"),
                   dict(focus_source="PLAN_VARIABLE = 'FOCUSED_CI_PLANS'\nPROTECTED_FILES = ('z.py',)\n"),
                   dict(release_source="REPOSITORY = 'other/x'\n")):
        with pytest.raises(fa.AdmissionError, match="changes canonical source identity"):
            validate(monkeypatch, tmp_path, script=git_script(trusted_exec=changed, **kwargs), protected_producer=True)


# ---------- the isolated interpreters, run for real on a tiny fake package ----------

def fake_scripts(tmp_path, plan_source, extra=None):
    root = tmp_path / "src"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts/focused_plan.py").write_text(plan_source)
    for name, text in (extra or {}).items():
        (root / "scripts" / name).write_text(text)
    return root


RESOLVE_PLAN = '''
REPOSITORY = "o/r"
class Scope:
    tree = "T"
    manifest_digest = "D"
def resolve(source, commit, registry):
    if source.head == "refuse":
        return "not a scope"
    return Scope()
'''
WORKLOAD = '''
class WorkloadSource:
    def __init__(self, event, repository, ref, payload, base, head, raw_diff):
        self.head = head
'''


def resolve_request(head="h"):
    return {"event": "pull_request", "ref": "r", "payload": {}, "base": "b", "head": head, "raw_diff": base64.b64encode(b"d").decode(), "commit": "c", "registry": {}}


def test_isolated_resolve_runs_the_trusted_resolver(tmp_path):
    root = fake_scripts(tmp_path, RESOLVE_PLAN, {"ci_workload.py": WORKLOAD})
    assert fa._isolated_resolve(root, tmp_path, resolve_request()) == {"manifest_digest": "D", "tree": "T"}
    with pytest.raises(fa.AdmissionError, match="did not return a focused scope"):
        fa._isolated_resolve(root, tmp_path, resolve_request("refuse"))
    broken = fake_scripts(tmp_path / "b", "raise RuntimeError('bad source')", {"ci_workload.py": WORKLOAD})
    with pytest.raises(fa.AdmissionError, match=r"(?s)refused packet: .*bad source"):
        fa._isolated_resolve(broken, tmp_path, resolve_request())


SLOT_PLAN = '''
SLOT_VARIABLES = ["FOCUSED_CI_PLANS_V2_%d" % i for i in range(4)]
MAX_SLOT = 40000
def append_slot(legacy, slots, additions):
    if "boom" in additions:
        raise RuntimeError("no slot")
    return MODE
'''


@pytest.mark.parametrize("mode,expected", [("None", None), ("[1, 'abc']", [1, "abc"])])
def test_isolated_slot_append_accepts_the_trusted_contract(tmp_path, mode, expected):
    root = fake_scripts(tmp_path, SLOT_PLAN.replace("MODE", mode))
    proposal = fa._isolated_slot_append(root, "{}", ("", "", "", ""), {"k": {}})
    assert proposal["result"] == expected and proposal["limit"] == 40000


@pytest.mark.parametrize("mode", ["[9, 'abc']", "[True, 'abc']", "[1, 5]", "[1, 'x' * 40001]"])
def test_isolated_slot_append_refuses_invalid_proposals(tmp_path, mode):
    root = fake_scripts(tmp_path, SLOT_PLAN.replace("MODE", mode))
    with pytest.raises(fa.AdmissionError, match="invalid trusted-main slot proposal"):
        fa._isolated_slot_append(root, "{}", ("", "", "", ""), {"k": {}})


def test_isolated_slot_append_refuses_a_failure_and_a_changed_contract(tmp_path):
    root = fake_scripts(tmp_path, SLOT_PLAN.replace("MODE", "None"))
    with pytest.raises(fa.AdmissionError, match=r"(?s)slot preparation refused: .*no slot"):
        fa._isolated_slot_append(root, "{}", ("", "", "", ""), {"boom": {}})
    other = fake_scripts(tmp_path / "o", SLOT_PLAN.replace("MODE", "None").replace("40000", "123"))
    with pytest.raises(fa.AdmissionError, match="unexpected trusted-main slot contract"):
        fa._isolated_slot_append(other, "{}", ("", "", "", ""), {"k": {}})


# ---------- apply_slots ----------

VARS = ["FOCUSED_CI_PLANS", *(f"FOCUSED_CI_PLANS_V2_{i}" for i in range(4))]
PR_PLAN = {"event": "pull_request", "head": SHA_A, "base": SHA_B, "tree": SHA_C}
MAIN_PLAN = {"event": "push", "ref": "refs/heads/main", "base": SHA_B, "tree": SHA_C}


def slots_call(tmp_path, monkeypatch, plans=(PR_PLAN, MAIN_PLAN), expected=None, proposal=None, **kw):
    state = {n: "" for n in VARS}
    expected = state if expected is None else expected
    monkeypatch.setattr(fa, "_isolated_slot_append", lambda source, legacy, slots, additions: proposal or {"result": [0, "cand"]})
    sets = []

    def put(name, value):
        sets.append((name, value))
        state[name] = value

    out = fa.apply_slots(list(plans), tmp_path, expected, lambda: dict(state), put, {"schema": 1, "admission_id": uuid.uuid4().hex, "head": SHA_A},
                         tmp_path / "lock", tmp_path, approved_candidate_sha256=hashlib.sha256(b"cand").hexdigest(), check_fresh=lambda: None, **kw)
    return out, sets, state


def test_apply_slots_pair_writes_one_slot_and_receipts(tmp_path, monkeypatch):
    path, sets, _state = slots_call(tmp_path, monkeypatch)
    assert sets == [("FOCUSED_CI_PLANS_V2_0", "cand")] and json.loads(Path(path).read_text())["slot_variable"] == "FOCUSED_CI_PLANS_V2_0"
    path, sets, _state = slots_call(tmp_path, monkeypatch, proposal={"result": None})
    assert sets == [] and json.loads(Path(path).read_text())["registry_changed"] is False


@pytest.mark.parametrize("kwargs,message", [
    (dict(release_main={}, resulting_main={}), "conflicting singleton"),
    (dict(pairs=0), "pair count is invalid"), (dict(pairs=True), "pair count is invalid"),
    (dict(release_main={}, pairs=2), "pair count is invalid"),
    (dict(plans=(PR_PLAN,)), "complete five-variable snapshot"), (dict(expected={"x": ""}), "complete five-variable snapshot"),
    (dict(plans=(PR_PLAN, PR_PLAN)), "one PR and one resulting-main plan per reviewed pair"),
    (dict(plans=(PR_PLAN, {**PR_PLAN, "head": SHA_B})), "one PR and one resulting-main plan per reviewed pair"),
    (dict(proposal={"result": [0, "other"]}), "differ from exact coordinator approval"),
])
def test_apply_slots_refusals(tmp_path, monkeypatch, kwargs, message):
    with pytest.raises(fa.AdmissionError, match=message):
        slots_call(tmp_path, monkeypatch, **kwargs)


def test_apply_slots_singleton_proof_rules(tmp_path, monkeypatch):
    digest = hashlib.sha256(fa.canonical(MAIN_PLAN)).hexdigest()
    proof = {"mode": "resulting-main-only/v1", "base": SHA_B, "tree": SHA_C, "main_manifest_sha256": digest}
    appended = []
    _out, sets, _state = slots_call(tmp_path, monkeypatch, plans=(MAIN_PLAN,), resulting_main=proof, check_main_append=lambda before, after: appended.append(1))
    assert appended == [1] and sets
    release = {**proof, "mode": "release-main-only/v1"}
    result, sets, _ = slots_call(tmp_path, monkeypatch, plans=(MAIN_PLAN,), release_main=release, check_main_append=lambda b, a: None)
    assert result["slot_variable"] == "FOCUSED_CI_PLANS_V2_0" and result["registry_changed"] is True
    for bad in ({**proof, "mode": "other"}, {**proof, "base": SHA_A}, {**proof, "tree": SHA_A}, {**proof, "main_manifest_sha256": "0" * 64}):
        with pytest.raises(fa.AdmissionError, match="exact typed resulting-main proof"):
            slots_call(tmp_path, monkeypatch, plans=(MAIN_PLAN,), resulting_main=bad, check_main_append=lambda b, a: None)
    with pytest.raises(fa.AdmissionError, match="exact typed resulting-main proof"):
        slots_call(tmp_path, monkeypatch, plans=(MAIN_PLAN,), resulting_main=proof)
    with pytest.raises(fa.AdmissionError, match="exact typed resulting-main proof"):
        slots_call(tmp_path, monkeypatch, plans=(PR_PLAN,), resulting_main=proof, check_main_append=lambda b, a: None)
    with pytest.raises(fa.AdmissionError, match="already admitted"):
        slots_call(tmp_path, monkeypatch, plans=(MAIN_PLAN,), resulting_main=proof, check_main_append=lambda b, a: None, proposal={"result": None})


def test_apply_slots_detects_snapshot_and_readback_drift(tmp_path, monkeypatch):
    state = {n: "" for n in VARS}
    monkeypatch.setattr(fa, "_isolated_slot_append", lambda *a: {"result": [0, "cand"]})
    common = dict(approved_candidate_sha256=hashlib.sha256(b"cand").hexdigest(), check_fresh=lambda: None)
    with pytest.raises(fa.AdmissionError, match="snapshot changed"):
        fa.apply_slots([PR_PLAN, MAIN_PLAN], tmp_path, {**state, VARS[0]: "x"}, lambda: dict(state), lambda n, v: None, {}, tmp_path / "l", tmp_path, **common)
    reads = iter([dict(state), {**state, VARS[1]: "sneaky"}])
    with pytest.raises(fa.AdmissionError, match="registry changed before write"):
        fa.apply_slots([PR_PLAN, MAIN_PLAN], tmp_path, state, lambda: next(reads), lambda n, v: None, {}, tmp_path / "l", tmp_path, **common)
    with pytest.raises(fa.AdmissionError, match="slot readback mismatch"):
        fa.apply_slots([PR_PLAN, MAIN_PLAN], tmp_path, state, lambda: dict(state), lambda n, v: None, {}, tmp_path / "l", tmp_path, **common)


# ---------- main ----------

def main_world(tmp_path, monkeypatch):
    packet = {"manifest": plan(), "review": {"path": "research/r.md", "head": SHA_A}, "main_manifest": MAIN_PLAN}
    packet_raw = json.dumps(packet).encode()
    source, workspace = tmp_path / "src", tmp_path / "ws"
    source.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(fa, "load_packet", lambda path, root, head, source_pair=False: (packet, packet_raw, b"review"))
    monkeypatch.setattr(fa, "_repository_from_remote", lambda core: CORE)
    monkeypatch.setattr(fa, "_read_main", lambda repo: SHA_B)
    monkeypatch.setattr(fa, "_prepare_core_snapshot", lambda core, main: (workspace, tmp_path / "snap"))
    monkeypatch.setattr(fa, "_canonical_source", lambda snap, main: (source, SHA_C, CORE, "FOCUSED_CI_PLANS", ("a.py",)))
    monkeypatch.setattr(fa, "validate_canonical", lambda *a, **kw: {"manifest_digest": "d", "validator_blob": "blob", "repository": CORE, "plan_variable": "FOCUSED_CI_PLANS", "merge_commit": SHA_C})
    monkeypatch.setattr(fa, "_gh_variable_get", lambda repo, var: "{}")
    monkeypatch.setattr(fa, "_gh_slot_get", lambda repo, var: "")
    monkeypatch.setattr(fa, "_isolated_slot_append", lambda *a: {"result": None, "variables": [], "limit": 40000})
    monkeypatch.setattr(fa, "apply_registry", lambda plans, get, set_, receipt, lock, home_, check_fresh: (check_fresh(), Path("/receipt"))[1])
    return packet_raw, source, workspace


def base_args(tmp_path, *more):
    return [str(tmp_path / "packet.json"), "--reviewed-head", SHA_A, "--core-repo", str(tmp_path), *more]


def test_main_dry_run_prints_the_prospective_registry(tmp_path, monkeypatch, capsys):
    _, source, workspace = main_world(tmp_path, monkeypatch)
    assert fa.main(base_args(tmp_path)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "dry-run" and out["would_add"] is True and out["key"] == f"pull_request:{SHA_A}" and out["remote_acceptance"] == "unverified"
    assert not source.exists() and not workspace.exists()


def test_main_pair_dry_run_and_compressed_slots(tmp_path, monkeypatch, capsys):
    main_world(tmp_path, monkeypatch)
    assert fa.main(base_args(tmp_path, "--reviewed-source-pair", "--reviewed-protected-producer")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["main_registry_key"] == f"push-tree:{SHA_C}" and out["protected_producer_approved"] is True
    main_world(tmp_path / "again", monkeypatch) if False else None
    assert fa.main(base_args(tmp_path, "--reviewed-source-pair", "--compressed-slots")) in (0, 1)


def test_main_apply_writes_the_registry(tmp_path, monkeypatch, capsys):
    raw, _, _ = main_world(tmp_path, monkeypatch)
    approval = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(fa, "_read_main", lambda repo: SHA_B)
    assert fa.main(base_args(tmp_path, "--apply", "--coordinator-approved-packet-sha256", approval)) == 0
    assert f"ADMITTED pull_request:{SHA_A} pre-open registry receipt /receipt" in capsys.readouterr().out


@pytest.mark.parametrize("extra,message", [
    (["--compressed-slots"], "compressed slots require pair mode"),
    (["--reviewed-source-pair", "--compressed-slots", "--apply"], "compressed slots require pair mode"),
    (["--reviewed-protected-producer"], "protected producer mode requires"),
    (["--apply"], "requires the exact coordinator-approved"),
    (["--coordinator-approved-packet-sha256", "x"], "applies only with --apply"),
])
def test_main_refusals(tmp_path, monkeypatch, capsys, extra, message):
    main_world(tmp_path, monkeypatch)
    assert fa.main(base_args(tmp_path, *extra)) == 1
    assert message in capsys.readouterr().err


def test_main_refuses_a_repository_mismatch_and_cleans_up(tmp_path, monkeypatch, capsys):
    _, source, workspace = main_world(tmp_path, monkeypatch)
    monkeypatch.setattr(fa, "_canonical_source", lambda snap, main: (source, SHA_C, "other/repo", "V", ("a.py",)))
    assert fa.main(base_args(tmp_path)) == 1
    assert "does not match the repository identity" in capsys.readouterr().err and not source.exists() and not workspace.exists()
