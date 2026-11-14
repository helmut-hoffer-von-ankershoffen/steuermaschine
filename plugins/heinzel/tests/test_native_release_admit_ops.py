"""The typed entry and the commands of the release-main route (release_ops.py, native/release_admit.py).

The moved operator tests are test_native_release_admit.py and test_native_release_admit_stuck.py.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import native
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.admission_lib import ops as admission
from steuermaschine_heinzel.native.release_admit_lib import focused_release, release_identity, release_ops
from steuermaschine_heinzel.native.release_admit_lib import release_slots as rs
from steuermaschine_heinzel.plugin import plugin

a = rs.a
NATIVE = Path(focused_release.__file__).parent.parent


def runtime():
    stack = stack_of(plugin)
    settings, _ = stack.validate({}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=fake_providers())


class Untouched:
    def __getattr__(self, name):
        raise AssertionError(f"the runtime was used ({name}) before the refusal")


# ----------------------------------------------------------------------------- port plan 3.1: pairs and release_main
PLAN = {"event": "push", "ref": "refs/heads/main", "base": "b" * 40, "tree": "c" * 40}
PROOF = {"mode": "release-main-only/v1", "base": "b" * 40, "tree": "c" * 40, "main_manifest_sha256": hashlib.sha256(a.canonical(PLAN)).hexdigest()}
EXPECTED = dict.fromkeys(rs.NAMES, "")


def slots(tmp_path, **kwargs):
    def unreachable(*args):
        raise AssertionError("the registry was reached")

    return a.apply_slots([PLAN], tmp_path, EXPECTED, unreachable, unreachable, {}, tmp_path / "lock", tmp_path,
                         approved_candidate_sha256="0" * 64, check_fresh=unreachable, **kwargs)


def test_release_main_with_a_pair_count_other_than_one_refuses(tmp_path):
    with pytest.raises(a.AdmissionError, match="pair count is invalid"):
        slots(tmp_path, release_main=PROOF, check_main_append=lambda before, after: None, pairs=2)


def test_release_main_and_resulting_main_together_refuse(tmp_path):
    with pytest.raises(a.AdmissionError, match="conflicting singleton admission proof types"):
        slots(tmp_path, release_main=PROOF, resulting_main=PROOF | {"mode": "resulting-main-only/v1"}, check_main_append=lambda b, c: None)


def test_release_main_needs_its_own_mode_and_the_exact_manifest(tmp_path):
    for proof in (PROOF | {"mode": "resulting-main-only/v1"}, PROOF | {"tree": "d" * 40}, PROOF | {"main_manifest_sha256": "0" * 64}):
        with pytest.raises(a.AdmissionError, match="exact typed resulting-main proof and one manifest"):
            slots(tmp_path, release_main=proof, check_main_append=lambda b, c: None)
    with pytest.raises(a.AdmissionError, match="exact typed resulting-main proof and one manifest"):
        slots(tmp_path, release_main=PROOF)  # no append check


# ----------------------------------------------------------------------------- port plan 3.3, 3.5, 3.6
def test_the_route_uses_the_one_consumer_check_of_the_pr_admission():
    from steuermaschine_heinzel.native.admission_lib import focused_slots

    assert rs.check_consumers is focused_slots.check_consumers and rs.read_stuck_queued is focused_slots.read_stuck_queued
    assert "def check_consumers" not in Path(rs.__file__).read_text()
    assert issubclass(rs.Runtime, focused_slots.Runtime)


@pytest.mark.parametrize("extra", [["--packets", "x"], ["--core-repos", "x"], ["--prs", "1"], ["--stuck-queued"], ["--cancel-blocking-pr-runs"],
                                   ["--pr", "5"], ["--reviewed-protected-producer"]])
def test_argv_form_excludes_every_other_admission_mode(tmp_path, capsys, extra):
    code = rs.main(["prepare", str(tmp_path / "p.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path), "--release-main-only", *extra])
    assert code == 1 and "release-main-only excludes" in capsys.readouterr().err


def test_argv_form_is_release_only(tmp_path, capsys):
    assert rs.main(["prepare", str(tmp_path / "p.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path)]) == 1
    assert "this entry admits release-main-only" in capsys.readouterr().err


def test_runtime_is_constructed_like_the_pr_admission_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(a, "_repository_from_remote", lambda core: "o/r")
    rt = rs.Runtime(tmp_path, tmp_path, github_host="github.com")
    assert rt.github_host == "github.com" and rt.repo == "o/r" and rt.cache == statedir.root()
    assert rt.service is None and rt.plist is None  # no queue_service_label: queue_state refuses, naming it
    assert rs.Runtime(tmp_path, tmp_path).github_host is None


def test_runtime_pins_the_github_host(tmp_path, monkeypatch):
    seen = []

    def run(args, **kwargs):
        seen.append((args, kwargs.get("env")))
        return rs.subprocess.CompletedProcess(args, 0, b"{}", b"")

    monkeypatch.setattr(rs.subprocess, "run", run)
    monkeypatch.setenv("GH_REPO", "someone/else")
    rt = rs.Runtime.__new__(rs.Runtime)
    rt.github_host, rt.repo = "github.com", "o/r"
    rt.run("gh", "api", "repos/o/r")
    rt.set_variable(rs.NAMES[1], "value")
    rt.run("git", "status")
    assert seen[0][0] == ("gh", "api", "repos/o/r", "--hostname", "github.com") and seen[0][1]["GH_HOST"] == "github.com" and "GH_REPO" not in seen[0][1]
    assert seen[1][0] == ("gh", "variable", "set", rs.NAMES[1], "-R", "github.com/o/r") and seen[2] == (("git", "status"), None)


def test_merge_freeze_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert rs.hzlib is release_identity and not release_identity.merge_frozen()
    (statedir.root() / "release-holds").mkdir(parents=True)
    assert not release_identity.merge_frozen()
    (statedir.root() / "release-holds/v1").touch()
    assert release_identity.merge_frozen()
    (statedir.root() / "release-holds/v1").unlink()
    (statedir.root() / "merge-freeze").symlink_to(tmp_path / "missing")
    assert release_identity.merge_frozen()  # a dangling link still freezes


# ----------------------------------------------------------------------------- the source binding
def test_every_bound_source_exists_and_the_validator_is_the_reviewed_file():
    assert all((NATIVE / name).is_file() for name in focused_release.SOURCES) and focused_release.VALIDATOR in focused_release.SOURCES
    listed = {name for name in focused_release.SOURCES if name.startswith("release_admit")}
    present = {"release_admit.py", *(f"release_admit_lib/{p.name}" for p in (NATIVE / "release_admit_lib").glob("*.py") if p.name != "__init__.py")}
    assert listed == present, "a module of the route is not bound into the transaction"
    # the semantic validator is byte-identical to the operator's reviewed file; release packets pin this digest
    assert hashlib.sha256((NATIVE / focused_release.VALIDATOR).read_bytes()).hexdigest().startswith("bc4ab4261e9fcf9a")


# ----------------------------------------------------------------------------- isolated start
def test_isolated_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(release_ops.site, "ENABLE_USER_SITE", False)
    monkeypatch.setattr(release_ops.sys, "path", ["/somewhere/site-packages"])
    for name in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONINSPECT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    assert release_ops.isolated_startup()
    for name in ("PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "PYTHONINSPECT"):
        monkeypatch.setenv(name, "x")
        assert not release_ops.isolated_startup()
        monkeypatch.delenv(name)
    monkeypatch.setattr(release_ops.site, "ENABLE_USER_SITE", True)
    assert not release_ops.isolated_startup()
    monkeypatch.setattr(release_ops.site, "ENABLE_USER_SITE", False)
    for entry in ("", str(tmp_path)):
        monkeypatch.setattr(release_ops.sys, "path", ["/somewhere/site-packages", entry])
        assert not release_ops.isolated_startup()


def test_prepare_and_apply_refuse_a_start_that_is_not_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(release_ops, "isolated_startup", lambda: False)
    with pytest.raises(a.AdmissionError, match="requires an isolated start"):
        release_ops.prepare(tmp_path / "p.json", tmp_path, tmp_path, runtime=Untouched())


# ----------------------------------------------------------------------------- stored arguments
def stored(tmp_path, **changes):
    directory = tmp_path / "packet"
    directory.mkdir()
    (directory / "packet.json").write_text("{}")
    receipts = [{"kind": "stuck-queued-pr-run", "run": 7}]
    nonconsumers = tmp_path / "nonconsumers.json"
    nonconsumers.write_text(json.dumps({"receipts": receipts}))
    prepared = {"schema": 1, "core_repo": str(tmp_path), "head": "a" * 40, "main": "b" * 40, "slot": rs.NAMES[1],
                "packet_sha256": hashlib.sha256(b"{}").hexdigest(), "nonconsumers": receipts,
                "release_main": {"mode": focused_release.MODE, "version": "0.9.40", "guard": {"recovery": None}}}
    (directory / admission.PREPARED_FILE).write_text(json.dumps(prepared))
    args = {"schema": 1, "route": focused_release.MODE, "sha256": admission.canonical_sha256(prepared), "packet": str((directory / "packet.json").resolve()),
            "core_repo": str(tmp_path), "review_root": str(tmp_path), "nonconsumers": str(nonconsumers)} | changes
    (directory / admission.ARGS_FILE).write_text(json.dumps(args))
    return directory / "packet.json", prepared, args


@pytest.mark.parametrize(("changes", "message"), [
    ({"route": "x"}, "not a release-main transaction"),
    ({"sha256": "0" * 64}, "another prepared transaction"),
    ({"core_repo": "/elsewhere"}, "core checkout"),
    ({"nonconsumers": None}, "nonconsumer receipts"),
])
def test_apply_refuses_changed_stored_arguments_before_anything_runs(tmp_path, changes, message):
    packet, prepared, _ = stored(tmp_path, **changes)
    with pytest.raises(a.AdmissionError, match=message):
        release_ops.apply(packet, admission.canonical_sha256(prepared), runtime=Untouched())


def test_apply_refuses_changed_packet_bytes_a_pr_transaction_and_a_wrong_digest(tmp_path):
    packet, prepared, _ = stored(tmp_path)
    with pytest.raises(a.AdmissionError, match="apply needs --approved"):
        release_ops.apply(packet, "nope", runtime=Untouched())
    with pytest.raises(a.AdmissionError, match="not the digest of the stored prepared transaction"):
        release_ops.apply(packet, "0" * 64, runtime=Untouched())
    packet.write_text("{ }")
    with pytest.raises(a.AdmissionError, match="packet bytes"):
        release_ops.apply(packet, admission.canonical_sha256(prepared), runtime=Untouched())
    packet.write_text("{}")
    pr_doc = {k: v for k, v in prepared.items() if k != "release_main"}
    (packet.parent / admission.PREPARED_FILE).write_text(json.dumps(pr_doc))
    with pytest.raises(a.AdmissionError, match="not a release-main transaction"):
        release_ops.apply(packet, admission.canonical_sha256(pr_doc), runtime=Untouched())


def test_apply_recomputes_compares_and_applies(tmp_path, monkeypatch):
    packet, prepared, args = stored(tmp_path)
    monkeypatch.setattr(release_ops, "isolated_startup", lambda: True)
    calls = []
    monkeypatch.setattr(rs, "prepare_release", lambda rt, path, root, nonconsumers=(): calls.append(("prepare", nonconsumers)) or (dict(prepared), {"p": 1}, None, None))
    monkeypatch.setattr(rs, "apply_release", lambda rt, prep, doc, source, approval, root: calls.append(("apply", approval, root)) or tmp_path / "receipt.json")
    result = release_ops.apply(packet, args["sha256"], runtime=object())
    assert calls == [("prepare", prepared["nonconsumers"]), ("apply", args["sha256"], tmp_path)] and result.receipt_path == str(tmp_path / "receipt.json")
    monkeypatch.setattr(rs, "prepare_release", lambda *x, **k: (prepared | {"slot": rs.NAMES[2]}, {}, None, None))
    monkeypatch.setattr(rs, "apply_release", lambda *x: pytest.fail("the write path was reached"))
    with pytest.raises(a.AdmissionError, match="prepared transaction changed; reprepare and review"):
        release_ops.apply(packet, args["sha256"], runtime=object())


def test_prepare_stores_transaction_and_arguments(tmp_path, monkeypatch):
    packet, prepared, args = stored(tmp_path)
    (packet.parent / admission.PREPARED_FILE).unlink()
    (packet.parent / admission.ARGS_FILE).unlink()
    monkeypatch.setattr(release_ops, "isolated_startup", lambda: True)
    monkeypatch.setattr(rs, "prepare_release", lambda rt, path, root, nonconsumers=(): (prepared, {}, None, None))
    result = release_ops.prepare(packet, tmp_path, tmp_path, nonconsumers=Path(args["nonconsumers"]), runtime=object())
    assert json.loads((packet.parent / admission.ARGS_FILE).read_text()) == args
    assert (packet.parent / admission.PREPARED_FILE).read_bytes() == a.canonical(prepared) + b"\n"
    assert result.sha256 == args["sha256"] and result.version == "0.9.40" and not result.recovery and "fresh release" in result.render()[2]


# ----------------------------------------------------------------------------- the commands
def test_both_commands_are_write_commands_under_one_review_file_and_unavailable_without_a_verdict(tmp_path, monkeypatch):
    from steuermaschine.native import NativeCommand

    monkeypatch.setattr(NativeCommand, "review_state", lambda self: ("missing", f"reviews/{self.review_file} is missing"))
    reg = {c.path: c for c in native.commands() if hasattr(c, "writes")}
    for path in ("release admit prepare", "release admit apply"):
        assert reg[path].writes and reg[path].review_file == "release-admit-apply.md"
    done = invoke(runtime(), ["release", "admit", "apply", str(tmp_path / "p.json"), "--approved", "0" * 64])
    assert done.exit_code == 70 and "release-admit-apply.md" in (done.stdout + done.stderr)
    done = invoke(runtime(), ["release", "admit", "prepare", str(tmp_path / "p.json"), "--core-repo", str(tmp_path), "--review-root", str(tmp_path)])
    assert done.exit_code == 70
    for path in (["release", "admit", "prepare"], ["release", "admit", "apply"]):
        text = invoke(runtime(), [*path, "--help"]).stdout
        assert "Changes:" in text and "Example: sm release admit" in text


def test_module_has_no_entry():
    for module in (rs, release_ops, focused_release, release_identity):
        assert "__main__" not in Path(module.__file__).read_text()
    assert sys.modules[rs.__name__] is rs
