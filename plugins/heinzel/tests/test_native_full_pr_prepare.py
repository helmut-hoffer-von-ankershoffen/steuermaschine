"""Full PR source approval is independent of CI proof and cannot overwrite registry state."""

import json
from contextlib import contextmanager

import pytest

from steuermaschine_heinzel.native.admission_lib import full_pr as f


@pytest.fixture
def world(tmp_path, monkeypatch):
    core = tmp_path / "core"
    core.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    packet = dict(
        schema=1,
        mode=f.MODE,
        core_repo=str(core),
        branch="fix/example",
        repository_id=123,
        verifier_blob="f" * 40,
        producer={"code": "original"},
        entry=dict(schema=1, kind="full-pr-reviewed", repository="owner/repo", head="a" * 40, base="b" * 40, tree="c" * 40, changed={"Makefile": "M"}, execution={"Makefile": "d" * 40}),
    )

    class Runtime:
        repo = "owner/repo"

        def __init__(self):
            self.core, self.cache = core, cache
            self.values, self.writes, self.guards = {}, [], []
            self.paused = False
            self.corrupt = False

        def variables(self):
            return dict(self.values), {}

        def set_variable(self, name, value):
            self.writes.append((name, value))
            self.values[name] = "corrupt" if self.corrupt else value

        @contextmanager
        def exclusions(self):
            self.paused = True
            try:
                yield
            finally:
                self.paused = False

        def queue_state(self):
            return None if self.paused else "state = running"

    rt = Runtime()

    @contextmanager
    def source(_):
        yield packet, tmp_path

    def append(_source, legacy, slots, additions):
        return {"result": [0, json.dumps(additions, sort_keys=True)]}

    def guard(runtime, desired, exceptions, paused, **kwargs):
        assert runtime.paused == paused
        runtime.guards.append((paused, kwargs.get("full")))

    monkeypatch.setattr(f, "source_packet", source)
    monkeypatch.setattr(f.a, "_isolated_slot_append", append)
    monkeypatch.setattr(f, "validate_registry", lambda *args, **kwargs: None)
    monkeypatch.setattr(f.slots, "global_guard", guard)
    monkeypatch.setattr(f.slots, "read_nonconsumers", lambda *_: {})
    monkeypatch.setattr(f, "local_identity", lambda _: (packet["entry"]["head"], packet["entry"]["base"], packet["branch"], packet["repository_id"]))
    monkeypatch.setattr(f, "producer_identity", lambda: packet["producer"])
    return rt, packet, tmp_path / "review"


def prepared(world):
    rt, _packet, out = world
    source = f.prepare(rt.core, out, rt=rt)
    assert source["stage"] == "source-review" and rt.writes == []
    second = f.prepare(rt.core, out, source["sha256"], rt=rt)
    assert second["stage"] == "apply-review" and rt.writes == []
    return second["sha256"]


def test_two_stage_exact_append_readback_receipt(world):
    rt, packet, out = world
    approved = prepared(world)
    receipt = f.apply(out, approved, rt=rt)
    assert len(rt.writes) == 1 and receipt["queue_restored"]
    assert receipt["head"] == packet["entry"]["head"]
    assert receipt["source_sha256"] == f.slots.digest(f.a.canonical(packet))
    assert (True, True) in rt.guards and (True, False) in rt.guards
    assert not rt.paused
    assert f.read(out / "attempt.json")["transaction_sha256"] == approved
    assert (out / "receipt.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(f.a.AdmissionError, match="attempt already"):
        f.apply(out, approved, rt=rt)
    assert len(rt.writes) == 1


@pytest.mark.parametrize("field", ["head", "base", "tree", "execution", "changed"])
def test_source_mutation_after_prepare_refuses(world, field):
    rt, packet, out = world
    approved = prepared(world)
    packet["entry"][field] = "changed"
    with pytest.raises(f.a.AdmissionError, match="source changed"):
        f.apply(out, approved, rt=rt)
    assert rt.writes == []


def test_registry_mutation_between_reviews_refuses(world):
    rt, _, out = world
    approved = prepared(world)
    rt.values[f.slots.NAMES[1]] = "another writer"
    with pytest.raises(f.a.AdmissionError, match="transaction changed"):
        f.apply(out, approved, rt=rt)
    assert rt.writes == []


def test_readback_failure_keeps_attempt_without_receipt_or_restore(world):
    rt, _, out = world
    approved = prepared(world)
    rt.corrupt = True
    with pytest.raises(f.a.AdmissionError, match="readback mismatch"):
        f.apply(out, approved, rt=rt)
    assert (out / "attempt.json").exists() and (out / "observed.json").exists()
    assert not (out / "receipt.json").exists() and len(rt.writes) == 1 and not rt.paused


def test_foreign_source_approval_never_prepares_slot(world):
    rt, _, out = world
    with pytest.raises(f.a.AdmissionError, match="source approval"):
        f.prepare(rt.core, out, "0" * 64, rt=rt)
    assert not (out / "prepare.out").exists() and rt.writes == []


def test_apply_digest_mutation_refuses_before_any_write(world):
    rt, _, out = world
    prepared(world)
    with pytest.raises(f.a.AdmissionError, match="digest differs"):
        f.apply(out, "0" * 64, rt=rt)
    assert rt.writes == []


def test_capacity_refusal_propagates_before_prepared_artifact(world, monkeypatch):
    rt, _, out = world
    source = f.prepare(rt.core, out, rt=rt)

    def busy(*args, **kwargs):
        raise f.a.AdmissionError("active consumer")

    monkeypatch.setattr(f.slots, "global_guard", busy)
    with pytest.raises(f.a.AdmissionError, match="active consumer"):
        f.prepare(rt.core, out, source["sha256"], rt=rt)
    assert not (out / "prepare.out").exists() and rt.writes == []


def test_deployed_verifier_missing_marker_fails_closed(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "focused_gate.py").write_text("# old protected verifier\n")
    monkeypatch.setattr(f.a, "_run", lambda *args, **kwargs: b"a" * 40)
    with pytest.raises(f.a.AdmissionError, match="deployed protected"):
        f.trusted_entry(tmp_path, tmp_path, "a" * 40, "b" * 40)


def test_review_directory_symlink_refused(world):
    rt, _, out = world
    out.symlink_to(rt.core, target_is_directory=True)
    with pytest.raises(f.a.AdmissionError, match="symlink"):
        f.prepare(rt.core, out, rt=rt)


def test_registry_race_inside_lock_refuses_without_write(world, monkeypatch):
    rt, _, out = world
    approved = prepared(world)
    original = rt.exclusions

    @contextmanager
    def changed():
        with original():
            rt.values[f.slots.NAMES[1]] = "concurrent writer"
            yield

    monkeypatch.setattr(rt, "exclusions", changed)
    with pytest.raises(f.a.AdmissionError, match="registry snapshot changed"):
        f.apply(out, approved, rt=rt)
    assert rt.writes == [] and not rt.paused


def test_unsupported_marker_without_validator_refuses(tmp_path, monkeypatch):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "focused_gate.py").write_text('FULL_PR_KIND = "full-pr-reviewed"\n')
    monkeypatch.setattr(f.a, "_run", lambda *args, **kwargs: b"a" * 40)
    with pytest.raises(f.a.AdmissionError, match="deployed protected"):
        f.trusted_entry(tmp_path, tmp_path, "a" * 40, "b" * 40)


def test_native_prepare_requires_registered_source_review_before_holds(tmp_path, monkeypatch):
    from steuermaschine.engine import Refuse
    from steuermaschine_heinzel.native import full_pr as command

    monkeypatch.setattr(command, "runtime", lambda: None)

    def denied(digest):
        assert digest == "a" * 64
        raise Refuse("independent source approval missing", "")

    monkeypatch.setattr(command.admission, "require_verdict", denied)
    monkeypatch.setattr(command.ops, "prepare", lambda *args: pytest.fail("unreviewed preparation ran"))
    with pytest.raises(Refuse, match="source approval missing"):
        command.prepare(tmp_path, tmp_path, "a" * 64)


def test_native_apply_requires_both_independent_verdicts(world, monkeypatch):
    from steuermaschine.engine import Refuse
    from steuermaschine_heinzel.native import full_pr as command

    _, _, out = world
    approved = prepared(world)
    source = f.read(out / "prepare.out")["source_sha256"]
    seen = []
    monkeypatch.setattr(command, "runtime", lambda: None)

    def verdict(digest):
        seen.append(digest)
        if digest == source:
            raise Refuse("independent source approval missing", "")

    monkeypatch.setattr(command.admission, "require_verdict", verdict)
    monkeypatch.setattr(command.ops, "apply", lambda *args: pytest.fail("unreviewed write ran"))
    with pytest.raises(Refuse, match="source approval missing"):
        command.apply(out, approved)
    assert seen == [approved, source]


def test_apply_rejects_changed_private_directory_permissions(world):
    rt, _, out = world
    approved = prepared(world)
    out.chmod(0o755)
    with pytest.raises(f.a.AdmissionError, match="owner-only"):
        f.apply(out, approved, rt=rt)
    assert rt.writes == []


def test_repository_replacement_before_write_refused(world, monkeypatch):
    rt, packet, out = world
    approved = prepared(world)
    monkeypatch.setattr(f, "local_identity", lambda _: (packet["entry"]["head"], packet["entry"]["base"], packet["branch"], 999))
    with pytest.raises(f.a.AdmissionError, match="source or producer changed"):
        f.apply(out, approved, rt=rt)
    assert rt.writes == []


@pytest.mark.parametrize("different_review_root", [False, True])
def test_ambiguous_write_cannot_replay_from_second_output_directory(world, monkeypatch, different_review_root):
    rt, packet, out = world
    second = out.with_name("second-review")
    common_root = out.parent
    source = f.prepare(rt.core, out, rt=rt)
    approved = f.prepare(rt.core, out, source["sha256"], review_root=common_root, rt=rt)["sha256"]
    second_source = f.prepare(rt.core, second, rt=rt)
    second_approved = f.prepare(rt.core, second, second_source["sha256"], review_root=second if different_review_root else common_root, rt=rt)["sha256"]
    assert (approved != second_approved) == different_review_root

    def ambiguous(name, value):
        # A remote write may have happened even though subsequent transport reads
        # still show the old snapshot. Never interpret that as safe to retry.
        rt.writes.append((name, value))

    monkeypatch.setattr(rt, "set_variable", ambiguous)
    with pytest.raises(f.a.AdmissionError, match="readback mismatch"):
        f.apply(out, approved, rt=rt)
    assert f.journal_path(rt, packet, "attempt").exists()
    assert f.journal_path(rt, packet, "observed").exists()
    assert not f.journal_path(rt, packet, "receipt").exists()
    with pytest.raises(f.a.AdmissionError, match="source admission attempt already"):
        f.apply(second, second_approved, rt=rt)
    assert len(rt.writes) == 1 and not rt.paused


def test_apply_sends_only_one_full_cancel_probe(world, monkeypatch):
    rt, _, out = world
    approved = prepared(world)
    checks = []

    def guard(runtime, desired, exceptions, paused, **kwargs):
        checks.append((paused, kwargs.get("full", False), kwargs.get("probe", True)))

    monkeypatch.setattr(f.slots, "global_guard", guard)
    f.apply(out, approved, rt=rt)
    assert checks.count((True, True, True)) == 1
    assert checks == [(False, False, False), (True, False, False), (True, True, True), (True, False, False)]


def test_verifier_runner_preserves_isolation_authority_and_deadlines(tmp_path, monkeypatch):
    from pathlib import Path

    calls = []

    class Runner:
        def run_bytes(self, argv, **kwargs):
            calls.append((argv, kwargs))
            return 124, b""

    monkeypatch.setattr(f, "Subprocess", Runner)
    monkeypatch.setattr(f.a, "_run", lambda *args, **kwargs: b"a" * 40)
    with pytest.raises(f.a.AdmissionError, match="deployed protected"):
        f.trusted_entry(tmp_path, tmp_path, "a" * 40, "b" * 40)
    values = dict.fromkeys(f.slots.NAMES, "")
    with pytest.raises(f.a.AdmissionError, match="protected registry"):
        f.validate_registry(tmp_path, values, {"head": "a" * 40}, present=False)
    assert [kwargs["timeout"] for _, kwargs in calls] == [90, 30]
    for argv, kwargs in calls:
        assert argv[1:3] == ["-I", "-c"]
        assert argv[4] == str(tmp_path)
        assert Path(kwargs["cwd"]) == tmp_path
        assert kwargs["env"] == {"PATH": f.os.defpath, "HOME": str(tmp_path)}
    request = json.loads(calls[1][1]["input"])
    assert request["key"] == "pull_request:" + "a" * 40
    assert request["slots"] == [""] * (len(f.slots.NAMES) - 1)
