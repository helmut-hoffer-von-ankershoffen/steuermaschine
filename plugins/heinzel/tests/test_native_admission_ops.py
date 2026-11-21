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












# ----------------------------------------------------------------------------- the replay driver is a script
