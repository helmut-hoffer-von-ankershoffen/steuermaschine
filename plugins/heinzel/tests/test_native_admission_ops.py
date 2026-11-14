"""The typed single-packet path (ops.py) and the native commands on top of it.

Fixtures under fixtures/admission/recorded were recorded 2026-11-12 from real admissions of the former tool
(`prepare > prepare.out`, `apply > apply.out`, receipts copied from the receipts directory), unstripped.
The offline parity tests replay those packets through the former tool and the native port (tests/admission_parity.py);
they need a Core Git repository that still holds the recorded commits: SM_ADMISSION_PARITY_OBJECTS=/path/to/core.
"""
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import focused_slots as s
from steuermaschine_heinzel.native.admission_lib import ops
from steuermaschine_heinzel.plugin import plugin

HERE = Path(__file__).parent
RECORDED = HERE / "fixtures/admission/recorded"
CASES = json.loads((RECORDED / "INDEX.json").read_text())
SINGLE = [c for c in CASES if (RECORDED / c["name"] / "packet.json").is_file()]
APPLIED = [c for c in SINGLE if c["receipt"]]
OBJECTS = os.environ.get("SM_ADMISSION_PARITY_OBJECTS", "")
OLD_TOOLS = Path(os.environ.get("SM_ADMISSION_PARITY_OLD_TOOLS", str(Path.home() / "Code/heinzel-management/tools")))
needs_objects = pytest.mark.skipif(not OBJECTS, reason="set SM_ADMISSION_PARITY_OBJECTS to a Core Git repository holding the recorded commits")


def runtime(allow_unreviewed=False):
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=allow_unreviewed)
    return Runtime(project, stack, settings, _providers=fake_providers())


def driver(impl, case, work):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "HZ_"))} | {"PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run([sys.executable, str(HERE / "admission_parity.py"), "--impl", impl, "--case", str(RECORDED / case["name"]),
                           "--objects", OBJECTS, "--work", str(work / impl), "--old-tools", str(OLD_TOOLS)],
                          capture_output=True, env=env, timeout=900)


# ----------------------------------------------------------------------------- parity on the recorded packets
@needs_objects
@pytest.mark.parametrize("case", SINGLE, ids=lambda c: c["name"])
def test_prepare_parity_old_tool_native_port_and_recorded_bytes(case, tmp_path):
    recorded = (RECORDED / case["name"] / "prepare.out").read_bytes()
    results = {impl: driver(impl, case, tmp_path) for impl in (("old",) if OLD_TOOLS.is_dir() else ()) + ("native", "native-argv")}
    for impl, done in results.items():
        assert done.returncode == 0, (impl, done.stderr.decode()[-2000:])
        assert done.stdout == recorded, f"{impl}: prepared transaction differs from the recorded one"
    assert len({done.stdout for done in results.values()}) == 1


@needs_objects
@pytest.mark.parametrize("case", APPLIED, ids=lambda c: c["name"])
def test_replayed_apply_writes_the_recorded_receipt(case, tmp_path):
    done = driver("native-apply", case, tmp_path)
    assert done.returncode == 0, done.stderr.decode()[-2000:]
    mine, real = json.loads(done.stdout), json.loads((RECORDED / case["name"] / "receipt.json").read_text())
    assert mine.pop("admission_id") != real.pop("admission_id")
    assert mine == real
    assert done.stdout == a.canonical(json.loads(done.stdout)) + b"\n"  # same file format as the former tool


# ----------------------------------------------------------------------------- hermetic: receipts and the registry variables
class Registry:
    """An in-memory registry seeded with a recorded transaction's `expected` values; records the one write."""

    def __init__(self, tmp_path, prepared):
        self.core, self.home, self.cache, self.management = tmp_path / "core", tmp_path, tmp_path / ".cache", tmp_path / "management"
        self.core.mkdir()
        self.cache.mkdir()
        self.repo, self.held_version_locks, self.paused, self.writes, self.events = prepared["repository"], None, False, [], []
        self.values = {n: prepared["expected"][n] for n in prepared["presence"]}
        self.main, self.head = prepared["main"], prepared["head"]

    def variables(self):
        return dict(self.values), {}

    def pages(self, path, key):
        return []

    def api(self, path):
        assert path.endswith("heads/main") or "/pulls" in path, path
        return {"object": {"sha": self.main}}

    def run(self, *args, **kwargs):
        assert args[:2] == ("gh", "api") and "pulls?state=open" in args[2], args
        return subprocess.CompletedProcess(args, 0, b"[]", b"")

    def queue_state(self):
        return None if self.paused else "state = running"

    def set_variable(self, name, value):
        self.events.append("write")
        self.writes.append((name, value))
        self.values[name] = value

    @contextmanager
    def exclusions(self):
        self.events.append("pause")
        self.paused, self.held_version_locks = True, []
        try:
            yield
        finally:
            self.paused, self.held_version_locks = False, None
            self.events.append("restore")


def replay(case, tmp_path, monkeypatch):
    directory = RECORDED / case["name"]
    prepared = json.loads((directory / "prepare.out").read_text())
    packet = json.loads((directory / "packet.json").read_text())
    rt = Registry(tmp_path, prepared)
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("HZ_QUEUE_STATE", "HZ_TRAIN_STATE_DIR", "HZ_RELEASE_DIR"):
        monkeypatch.delenv(name, raising=False)

    def git(argv, **kwargs):
        if argv == ["git", "branch", "--show-current"]:
            return b"parity\n"
        assert argv == ["git", "rev-parse", "HEAD"], argv
        return rt.head.encode()

    monkeypatch.setattr(a, "_run", git)
    slot = prepared["slot"]

    def append(source, legacy, slots, additions):  # the trusted-main codec, answered with the recorded proposal
        assert legacy == prepared["expected"][s.NAMES[0]] and slots == tuple(prepared["expected"][n] for n in s.NAMES[1:])
        assert set(additions) == {a.registry_key(packet["manifest"]), a.registry_key(packet["main_manifest"])}
        return {"result": [s.NAMES.index(slot) - 1, prepared["desired"][slot]]}

    monkeypatch.setattr(a, "_isolated_slot_append", append)
    return rt, prepared, packet


@pytest.mark.parametrize("case", APPLIED, ids=lambda c: c["name"])
def test_apply_of_a_recorded_transaction_reproduces_the_real_receipt(case, tmp_path, monkeypatch):
    """The native apply writes the receipt the former tool wrote: same directory, file name, bytes layout and fields."""
    rt, prepared, packet = replay(case, tmp_path, monkeypatch)
    real_path = Path((RECORDED / case["name"] / "apply.out").read_text().strip())
    real = json.loads((RECORDED / case["name"] / "receipt.json").read_text())
    path = s.apply_prepared(rt, prepared, packet, rt.core, {}, ops.canonical_sha256(prepared))
    assert rt.events == ["pause", "write", "restore"]
    assert rt.writes == [(prepared["slot"], prepared["desired"][prepared["slot"]])]
    assert {n: rt.values.get(n, "") for n in s.NAMES} == prepared["desired"]  # the five registry variables, read back
    assert path.parent == statedir.root() / "focused-admission-receipts" and real_path.parent.name.endswith("focused-admission-receipts")
    mine = json.loads(path.read_text())
    assert path.name == f"head-{prepared['head']}-{mine['admission_id']}.json" and real_path.name.startswith(f"head-{prepared['head']}-")
    assert path.read_bytes() == a.canonical(mine) + b"\n"
    assert {k: v for k, v in mine.items() if k != "admission_id"} == {k: v for k, v in real.items() if k != "admission_id"}


def test_a_changed_registry_value_refuses_before_the_write(tmp_path, monkeypatch):
    rt, prepared, packet = replay(APPLIED[0], tmp_path, monkeypatch)
    rt.values[s.NAMES[0]] += " "
    with pytest.raises(a.AdmissionError, match="registry snapshot changed"):
        s.apply_prepared(rt, prepared, packet, rt.core, {}, ops.canonical_sha256(prepared))
    assert rt.writes == [] and rt.events == ["pause", "restore"]
    assert not list(tmp_path.glob(".cache/hz-focused-admission-receipts/*.json"))


# ----------------------------------------------------------------------------- stored arguments
class Untouched:
    """A runtime that must not be used: apply has to refuse before any read or write."""

    def __getattr__(self, name):
        raise AssertionError(f"apply touched the runtime ({name}) before it refused")


def stored(tmp_path, case=None, **changes):
    case = case or APPLIED[0]
    directory = tmp_path / "packet"
    directory.mkdir()
    for name in ("packet.json", "prepare.out"):
        (directory / name).write_bytes((RECORDED / case["name"] / name).read_bytes())
    prepared = json.loads((directory / "prepare.out").read_text())
    nonconsumers = tmp_path / "nonconsumers.json"
    nonconsumers.write_text(json.dumps({"receipts": prepared["nonconsumers"]}))
    args = dict(schema=1, sha256=ops.canonical_sha256(prepared), packet=str((directory / "packet.json").resolve()), core_repo=str(tmp_path),
                review_root=str(tmp_path), pr=prepared["pr"], protected=prepared["protected_producer"], nonconsumers=str(nonconsumers),
                expected_remote_head=None, resulting_main_only=False) | changes
    (directory / ops.ARGS_FILE).write_text(json.dumps(args))
    return directory / "packet.json", prepared, args


@pytest.mark.parametrize(("changes", "message"), [
    (dict(pr=5), "pr"),
    (dict(protected="flip"), "protected producer"),
    (dict(resulting_main_only=True), "differ from the reviewed transaction: resulting-main-only"),
    (dict(expected_remote_head="a" * 40), "differ from the reviewed transaction: expected remote head"),
    (dict(nonconsumers=None), "nonconsumer receipts"),
    (dict(sha256="0" * 64), "another prepared transaction"),
    (dict(schema=2), "another packet"),
    (dict(packet="/elsewhere/packet.json"), "another packet"),
])
def test_apply_refuses_changed_stored_arguments_before_anything_runs(tmp_path, changes, message):
    packet, prepared, args = stored(tmp_path)
    if changes.get("protected") == "flip":
        changes = dict(protected=not prepared["protected_producer"])
    (packet.parent / ops.ARGS_FILE).write_text(json.dumps(args | changes))
    with pytest.raises(a.AdmissionError, match=message):
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=Untouched())


def test_apply_refuses_a_changed_packet_or_another_receipts_file(tmp_path):
    packet, prepared, args = stored(tmp_path)
    Path(args["nonconsumers"]).write_text(json.dumps({"receipts": prepared["nonconsumers"][:-1]}))
    with pytest.raises(a.AdmissionError, match="nonconsumer receipts"):
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=Untouched())
    Path(args["nonconsumers"]).write_text(json.dumps({"receipts": prepared["nonconsumers"]}))
    packet.write_bytes(packet.read_bytes() + b"\n")
    with pytest.raises(a.AdmissionError, match="packet bytes"):
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=Untouched())


def test_apply_needs_the_exact_approved_digest_and_stored_arguments(tmp_path):
    packet, prepared, _ = stored(tmp_path)
    for bad in ("", ops.canonical_sha256(prepared)[:-1], ops.canonical_sha256(prepared).upper(), None):
        with pytest.raises(a.AdmissionError, match="apply needs --approved with the sha256"):
            ops.apply(packet, bad, runtime=Untouched())
    with pytest.raises(a.AdmissionError, match="not the digest of the stored prepared transaction"):
        ops.apply(packet, "0" * 64, runtime=Untouched())
    (packet.parent / "prepare.out").write_text(json.dumps(prepared | {"pr": 9}))
    with pytest.raises(a.AdmissionError, match="not the digest of the stored prepared transaction"):
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=Untouched())
    (packet.parent / ops.ARGS_FILE).unlink()
    with pytest.raises(a.AdmissionError, match="run `sm head prepare`"):
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=Untouched())


def test_apply_refuses_a_batch_transaction(tmp_path):
    packet, _, _ = stored(tmp_path)
    batch = json.loads((RECORDED / "batch1" / "prepare.out").read_text())
    (packet.parent / "prepare.out").write_text(json.dumps(batch))
    with pytest.raises(a.AdmissionError, match="batch transaction"):
        ops.apply(packet, ops.canonical_sha256(batch), runtime=Untouched())


def test_apply_refuses_a_worktree_that_left_the_reviewed_head_and_names_the_fix(tmp_path, monkeypatch):
    packet, prepared, _ = stored(tmp_path)
    monkeypatch.setattr(a, "_run", lambda argv, **kw: b"d" * 40 + b"\n")
    with pytest.raises(a.AdmissionError) as refused:
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=Untouched())
    text = str(refused.value)
    assert f"is at {'d' * 12}" in text and f"reviewed head is {prepared['head'][:12]}" in text
    assert f"worktree add -b admit/{prepared['head'][:12]} ../admit-{prepared['head'][:12]} {prepared['head']}" in text and "do not move this worktree back" in text


def test_prepare_stores_the_transaction_and_its_arguments_next_to_the_packet(tmp_path, monkeypatch):
    packet, prepared, args = stored(tmp_path)
    (packet.parent / "prepare.out").unlink()
    (packet.parent / ops.ARGS_FILE).unlink()
    seen = {}

    def fake_prepare(rt, packet_path, review_root, **kwargs):
        seen.update(kwargs, packet=packet_path, review_root=review_root)
        return prepared, {}, None, None, {}

    monkeypatch.setattr(s, "prepare", fake_prepare)
    result = ops.prepare(packet, tmp_path, tmp_path, nonconsumers=Path(args["nonconsumers"]), protected=prepared["protected_producer"], runtime=object())
    assert seen["nonconsumers"] == prepared["nonconsumers"] and seen["pr"] is None and seen["protected"] is prepared["protected_producer"]
    assert (packet.parent / "prepare.out").read_bytes() == (RECORDED / APPLIED[0]["name"] / "prepare.out").read_bytes()
    assert json.loads((packet.parent / ops.ARGS_FILE).read_text()) == args
    assert result.sha256 == args["sha256"] and result.slot == prepared["slot"] and result.render()[1] == f"sha256 {result.sha256}"
    # what prepare stored is exactly what apply accepts up to the first real read
    monkeypatch.setattr(a, "_run", lambda argv, **kw: b"d" * 40)
    with pytest.raises(a.AdmissionError, match="worktree"):
        ops.apply(packet, result.sha256, runtime=Untouched())


def test_apply_refuses_when_the_recomputed_transaction_differs_and_never_reaches_the_write(tmp_path, monkeypatch):
    packet, prepared, _ = stored(tmp_path)
    monkeypatch.setattr(a, "_run", lambda argv, **kw: prepared["head"].encode())
    changed = prepared | {"desired": prepared["desired"] | {s.NAMES[4]: "x"}}
    monkeypatch.setattr(s, "prepare", lambda *args, **kwargs: (changed, {}, None, tmp_path / "workspace", {}))
    monkeypatch.setattr(s, "apply_prepared", lambda *args, **kwargs: pytest.fail("the write path was reached"))
    with pytest.raises(a.AdmissionError, match="prepared transaction changed; reprepare and review"):
        ops.apply(packet, ops.canonical_sha256(prepared), runtime=object())


def test_apply_reprepares_with_the_probe_and_the_stored_arguments(tmp_path, monkeypatch):
    packet, prepared, args = stored(tmp_path)
    monkeypatch.setattr(a, "_run", lambda argv, **kw: prepared["head"].encode())
    seen = {}

    def fake_prepare(rt, packet_path, review_root, **kwargs):
        seen.update(kwargs, packet=packet_path, review_root=review_root)
        return prepared, {"p": 1}, None, tmp_path / "workspace", {"proofs": 1}

    def fake_apply(rt, prepared_, packet_doc, source, proofs, approval, *, snapshot):
        seen.update(approval=approval, snapshot=snapshot, proofs=proofs, same=prepared_ is prepared)
        return tmp_path / "receipt.json"

    monkeypatch.setattr(s, "prepare", fake_prepare)
    monkeypatch.setattr(s, "apply_prepared", fake_apply)
    result = ops.apply(packet, args["sha256"], runtime=object())
    assert "probe" not in seen  # the moved default (probe=True) stays for apply
    assert seen["nonconsumers"] == prepared["nonconsumers"] and seen["protected"] is prepared["protected_producer"] and seen["pr"] is None
    assert seen["approval"] == args["sha256"] and seen["snapshot"] == tmp_path / "workspace" / "repo" and seen["same"] and seen["proofs"] == {"proofs": 1}
    assert result.receipt_path == str(tmp_path / "receipt.json") and result.render() == [result.receipt_path]


def test_the_command_form_of_prepare_sends_no_cancel_probe(tmp_path, monkeypatch):
    packet, prepared, args = stored(tmp_path)
    seen = {}
    monkeypatch.setattr(s, "prepare", lambda rt, *a_, **kwargs: seen.update(kwargs) or (prepared, {}, None, None, {}))
    ops.prepare(packet, tmp_path, tmp_path, nonconsumers=Path(args["nonconsumers"]), protected=prepared["protected_producer"], runtime=object())
    assert seen["probe"] is False


def test_moved_prepare_passes_probe_to_its_guard(monkeypatch):
    import inspect

    source = inspect.getsource(s.prepare)
    assert "resulting_main_only=False, *, probe=True, repair_main=False):" in source
    assert "resulting_main=main_proof, probe=probe, repair_main=repair_proof)" in source
    seen = []
    monkeypatch.setattr(s, "packet_guard", lambda *args, **kwargs: None)
    monkeypatch.setattr(s, "global_guard", lambda rt, desired, exceptions, paused, *, probe=True, full=False: seen.append(probe))
    s.fresh_guard(None, None, None, None, None, False, probe=False)
    s.fresh_guard(None, None, None, None, None, False)
    assert seen == [False, True]


def test_exclusive_modes_refuse(tmp_path):
    with pytest.raises(a.AdmissionError, match="--expected-remote-head excludes --resulting-main-only"):
        ops.prepare(tmp_path / "packet.json", tmp_path, tmp_path, pr=5, expected_remote_head="a" * 40, resulting_main_only=True, runtime=Untouched())


def test_the_repository_identity_is_the_reviewed_constant(monkeypatch):
    from steuermaschine_heinzel.native.admission_lib import identity

    assert identity.CORE == "helmut-hoffer-von-ankershoffen/heinzel" == a._management_core() == s.hzlib.CORE
    assert identity.SHORT == {"": identity.CORE, "core": identity.CORE, "plugins": identity.CORE + "-plugins",
                              "acme": identity.CORE + "-acme", "sf": identity.CORE + "-sf"}
    assert identity.split_key("393") == (identity.CORE, "393", "393") and identity.split_key("#393")[1] == "393"
    assert identity.split_key("plugins #38") == (identity.PLUGINS, "38", "plugins #38") and identity.split_key(["plugins", "38"])[2] == "plugins #38"
    for bad in ("x 1", "abc", "plugins x"):
        with pytest.raises(ValueError):
            identity.split_key(bad)
    for bad in ("norepo", "a/b/c", None):
        monkeypatch.setattr(identity, "CORE", bad)
        with pytest.raises(a.AdmissionError, match="repository identity is malformed"):
            a._management_core()


def test_prepare_refuses_bad_options_before_any_read(tmp_path):
    for kwargs, message in ((dict(pr=0), "PR must be positive"), (dict(resulting_main_only=True), "requires a PR"),
                            (dict(pr=5, expected_remote_head="short"), "expected remote head")):
        with pytest.raises(a.AdmissionError, match=message):
            ops.prepare(tmp_path / "packet.json", tmp_path, tmp_path, runtime=Untouched(), **kwargs)
    assert not list(tmp_path.iterdir())


def test_blockers_lists_unclaimed_active_runs():
    class Rt:
        repo = "o/r"

        def pages(self, path, key):
            state = path.split("status=")[-1]
            return [dict(id=7, status="queued", path=".github/workflows/ci.yml", head_sha="a" * 40, head_branch="x", event="pull_request",
                         run_attempt=1, workflow_id=3)] if state == "queued" else []

    result = ops.blockers(Path("."), runtime=Rt())
    assert [r.id for r in result.blocking] == [7] and result.render() == [f"7 queued pull_request x .github/workflows/ci.yml {'a' * 8}"]
    assert ops.BlockersResult(blocking=[]).render() == ["no active run blocks apply"]


# ----------------------------------------------------------------------------- the commands
def test_head_hash_text_and_json():
    path = RECORDED / APPLIED[0]["name"] / "prepare.out"
    expected = json.loads((RECORDED / APPLIED[0]["name"] / "receipt.json").read_text())["transaction_sha256"]
    text = invoke(runtime(), ["head", "hash", str(path)])
    assert text.exit_code == 0 and text.stdout.strip() == expected
    doc = invoke(runtime(), ["--format", "json", "head", "hash", str(path)])
    assert doc.exit_code == 0 and ops.HashResult.model_validate(json.loads(doc.stdout)).sha256 == expected


def test_head_hash_refuses_a_file_that_is_not_json(tmp_path):
    (tmp_path / "x").write_text("not json")
    done = invoke(runtime(), ["head", "hash", str(tmp_path / "x")])
    assert done.exit_code == 1 and "focused admission refused" in (done.stdout + done.stderr)


def test_head_apply_command_reaches_the_guards_when_tests_allow_it(tmp_path):
    packet, prepared, _ = stored(tmp_path, pr=5)
    sha = ops.canonical_sha256(prepared)
    rt = runtime(allow_unreviewed=True)
    rt.providers.store.put_receipt("verdicts", sha, {"verdict": "approve", "reviewer": "reviewer-1"})  # apply needs a recorded verdict
    done = invoke(rt, ["head", "apply", str(packet), "--approved", sha])
    assert done.exit_code == 1 and "stored prepare arguments differ from the reviewed transaction: pr" in (done.stdout + done.stderr)
    doc = invoke(rt, ["--format", "json", "head", "apply", str(packet), "--approved", ops.canonical_sha256(prepared)])
    assert doc.exit_code == 1 and "error" in json.loads(doc.stdout)


def test_head_prepare_wait_exits_75_and_names_the_run(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "blockers", lambda core, nonconsumers=None, runtime=None: ops.BlockersResult(blocking=[
        ops.BlockingRun(id=43307443066, status="in_progress", event="push", head_branch="main", path=".github/workflows/ci.yml", head_sha="a" * 40)]))
    monkeypatch.setattr(ops, "prepare", lambda *args, **kwargs: pytest.fail("prepare ran while a run blocks"))
    rt = runtime(True)
    done = invoke(rt, ["head", "prepare", str(tmp_path / "packet.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path), "--wait"])
    text = done.stdout + done.stderr
    assert done.exit_code == 75 and "uncovered active run 43307443066 in_progress main (push)" in text
    assert "waiting (0 min) for uncovered active run 43307443066 in_progress main (push); next look in 90 s" in done.stderr
    assert sum(rt.providers.clock.slept) == 45 * 60  # the configured limit, slept on the fake clock only


def test_head_prepare_without_wait_refuses_at_once_with_the_blockers_hint(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise a.AdmissionError("unknown active Actions run; admission refused: 9 queued pull_request x .github/workflows/ci.yml")

    monkeypatch.setattr(ops, "prepare", refuse)
    done = invoke(runtime(True), ["head", "prepare", str(tmp_path / "packet.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path)])
    assert done.exit_code == 1 and "sm head blockers" in (done.stdout + done.stderr)


def test_a_timeout_of_a_process_is_a_refusal_not_a_traceback(tmp_path, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh"], 30)

    monkeypatch.setattr(ops, "prepare", slow)
    done = invoke(runtime(True), ["--format", "json", "head", "prepare", str(tmp_path / "p.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path)])
    assert done.exit_code == 1 and "focused admission refused" in json.loads(done.stdout)["error"]


def test_head_prepare_and_blockers_commands_exist_with_contract_help():
    for path in (["head", "prepare"], ["head", "blockers"], ["head", "apply"], ["head", "hash"]):
        done = invoke(runtime(), [*path, "--help"])
        assert done.exit_code == 0 and "Changes:" in done.stdout and "Example: sm " in done.stdout


# ----------------------------------------------------------------------------- the replay driver is a script
def test_the_replay_driver_runs_as_a_script(tmp_path):
    """`focused_resulting.replay_original` copies focused_replay.py next to an archived `scripts/` directory and runs the
    copy as a script. This runs that copy for real, as a process, against a stand-in `scripts` package: the file must
    perform the replay when executed (its `__main__` block), read the request on stdin and print the proof.
    Not covered here: the `uv run --script` launcher and the real archived judge (network, token)."""
    import hashlib
    import io
    import zipfile

    from steuermaschine_heinzel.native.admission_lib import focused_resulting as resulting

    source = Path(resulting.__file__).with_name("focused_replay.py")
    assert "if __name__ == '__main__':\n    main()" in source.read_text()
    driver = tmp_path / "resulting-main-replay.py"
    driver.write_bytes(source.read_bytes())
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/focused_gate.py").write_text(
        "import json\nMAX_UPLOAD_LOG = MAX_ARCHIVE = 1 << 20\nREPORT = 'report.json'\n"
        "def require(ok, message):\n    if not ok: raise SystemExit(message)\n"
        "def parse(data): return json.loads(data)\n"
        "def verify(gate, run, manifest, read):\n    read('actions/artifacts/1/zip'); return 'verified'\n")
    (tmp_path / "scripts/merge_gate.py").write_text(
        "import pathlib\nGITHUB = 'https://api.github.invalid'\n"
        "def Gate(*args): return args\n"
        "def fetch_evidence(url, token, limit):\n"
        "    assert token == 'fixture-token'\n"
        "    return (pathlib.Path(__file__).parent.parent / 'evidence' / url.split('/repos/o/r/')[1].replace('/', '_')).read_bytes()\n")
    original = {"head": "a" * 40, "pr": 5, "ci_run": 77, "judge_source": "b" * 40, "tested_commit": "c" * 40, "tested_tree": "d" * 40,
                "tested_base": "e" * 40, "manifest_sha256": "f" * 64}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("report.json", json.dumps({"commit": original["tested_commit"], "tree": original["tested_tree"],
                                                   "base": original["tested_base"], "manifest_sha256": original["manifest_sha256"]}))
    blob = archive.getvalue()
    live = {"checks": {"merge-gate": 1, "coverage-floor": 2},
            "artifacts": {"1": {"size_in_bytes": len(blob), "digest": "sha256:" + hashlib.sha256(blob).hexdigest()}}}

    def log(*lines):
        return "".join(f"2026-11-12T00:00:00Z {line}\n" for line in lines).encode()

    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "checkout_logs").write_bytes(log("[command]/usr/bin/git log -1 --format=%H", original["judge_source"]))
    (evidence / "coverage_logs").write_bytes(log("##[group]Run uv run --no-build --frozen --script scripts/focused_gate.py",
                                                 f"  PR_HEAD_SHA: {original['head']}", "  PR_NUMBER: 5", "  CI_RUN_ID: 77"))
    (evidence / "report_logs").write_bytes(log("  MERGE_GATE: 1", "  COVERAGE_FLOOR: 2"))
    (evidence / "actions_artifacts_1_zip").write_bytes(blob)
    request = {"token": "fixture-token", "repository": "o/r", "original": original, "plan": {"x": 1}, "live": live,
               "paths": {"checkout": "checkout/logs", "coverage": "coverage/logs", "report": "report/logs"}}
    done = subprocess.run([sys.executable, "-I", str(driver)], input=a.canonical(request), capture_output=True, cwd=tmp_path, check=False)
    assert done.returncode == 0, done.stderr.decode()
    proof = json.loads(done.stdout)
    assert proof["tested_commit"] == original["tested_commit"] and proof["verdict"] == "verified" and proof["artifacts"] == live["artifacts"]
    assert set(proof["judge_log_sha256"]) == {"checkout", "coverage", "report"}
    # a refusal of the archived reader ends the script with a non-zero exit, which replay_original turns into a refusal
    (evidence / "report_logs").write_bytes(log("  MERGE_GATE: 9", "  COVERAGE_FLOOR: 2"))
    refused = subprocess.run([sys.executable, "-I", str(driver)], input=a.canonical(request), capture_output=True, cwd=tmp_path, check=False)
    assert refused.returncode != 0 and b"does not bind the required proof" in refused.stderr and not refused.stdout
