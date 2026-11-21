"""Pipelined release train step 1 (research/pipelined-release-train-2026-11-07.md): the freeze and the gate hold end
at the tag, only an open release/* PR or a receipt unfinished before its tag blocks a cut, state per version."""

import json
import sys
from pathlib import Path

import pytest
from steuermaschine_heinzel import state as statedir

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def train(tool, tmp_path, monkeypatch):
    mod = tool("hz-train")
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "train")
    monkeypatch.setattr(mod, "AUTO_CUT", True)  # the cut rules below; auto-cut off has its own tests
    return mod


LEGACY = {"attempts": 2, "phase": "release", "version": "0.9.29", "previous": "0.9.28", "start": 1.0,
          "last_start": 1.0, "last_version": None, "token": "99beae51615c8eb8", "intro": "x", "tip": "abc"}


def test_receipt_blocks_only_before_its_tag(train):
    w = train.Fake()
    assert train.receipt_blocks(w, {"version": "0.9.29", "freeze_token": "t"})
    w.tags = {"v0.9.29"}
    assert not train.receipt_blocks(w, {"version": "0.9.29", "freeze_token": "t"})
    assert not train.receipt_blocks(w, {"version": "0.9.29", "finished": True})

    class Broken(train.Fake):
        def has_tag(self, tag):
            raise RuntimeError("gh down")
    assert train.receipt_blocks(Broken(), {"version": "0.9.29"}), "unreadable tag state fails closed"


def test_a_foreign_release_past_its_tag_does_not_block_the_cut(train):
    # 2026-11-08 design: N+1 no longer waits for N's tail in hz-train; it waits for N's acceptance DISPATCH (merge
    # window), then runs hz-release, which itself waits at `previous release accepted` before merging.
    w = train.Fake(rcpt={"version": "0.9.27", "freeze_token": "t"}, tags={"v0.9.27"})
    assert train.tick(w) == 0 and "merge window" in w.out[-1] and not w.calls
    w.rcpt = {"version": "0.9.27", "freeze_token": "t", "acceptance_requested": True}
    assert train.tick(w) == 0 and ("release", "0.9.28", "Thin scrollbars; Clear button.") in w.calls


def test_a_foreign_release_before_its_tag_is_never_touched(train):
    w = train.Fake(rcpt={"version": "0.9.28", "freeze_token": "t"})
    assert train.tick(w) == 0 and not w.calls and "before its tag" in w.out[-1]


def test_only_an_open_release_pr_skips(train):
    w = train.Fake(active=1)
    assert train.tick(w) == 0 and not w.calls and w.out[-1] == "skip: a release/* PR is open"
    assert not hasattr(train.Real, "release_activity"), "release/* runs (N's tail) no longer block"


def test_the_previous_tail_unfinished_waits_without_burning_an_attempt(train):
    w = train.Fake(release_result=(1, "STOPPED: release 0.9.27 is unfinished; finish it before starting 0.9.28. Run the same command again"))
    assert train.tick(w) == 0 and "wait" in w.out[-1]
    assert train.load_state()["attempts"] == 0 and train.load_state()["phase"] == "release"


def test_tag_step_line_detection(train):
    assert train.tag_step_done("ok   tag\n") and train.tag_step_done("ok   tag (already)")
    assert not train.tag_step_done("ok   main gate (train path)") and not train.tag_step_done("wait tag 3m")


def test_state_per_version_and_cadence_index(train):
    w = train.Fake()
    assert train.tick(w) == 0
    done = json.loads((train.STATE_DIR / "v0.9.28.json").read_text())
    assert done["phase"] == "done" and done["version"] == "0.9.28"
    index = json.loads((train.STATE_DIR / "train.json").read_text())
    assert index["last_version"] == "0.9.28" and "phase" not in index
    assert train.load_state()["phase"] == "idle"
    assert not (train.STATE_DIR / "state.json").exists()


def test_blocked_and_clear_keep_the_version_file(train):
    w = train.Fake(release_result=(1, "NOT ACCEPTED 0.9.28 (run 42)"))
    assert train.tick(w) == 3
    assert json.loads((train.STATE_DIR / "v0.9.28.json").read_text())["phase"] == "blocked"
    train.clear()
    assert json.loads((train.STATE_DIR / "v0.9.28.json").read_text())["phase"] == "cleared"
    assert train.load_state()["phase"] == "idle"


def test_legacy_state_is_read_as_is_and_migrated_unchanged(train):
    train.STATE_DIR.mkdir(parents=True)
    legacy = train.STATE_DIR / "state.json"
    legacy.write_text(json.dumps(LEGACY))
    (train.STATE_DIR / "v0.9.29.log").write_text("ok   tag (already)\n")
    assert train.load_state() == LEGACY, "status and a dry-run read the legacy file without changing it"
    assert legacy.exists()
    assert "v0.9.29" in train.migrate_legacy()
    assert json.loads((train.STATE_DIR / "v0.9.29.json").read_text()) == LEGACY
    assert json.loads((train.STATE_DIR / "train.json").read_text())["last_start"] == 1.0
    assert not legacy.exists() and (train.STATE_DIR / "state.json.migrated").exists()
    assert (train.STATE_DIR / "v0.9.29.log").read_text() == "ok   tag (already)\n"
    assert train.load_state() == LEGACY
    assert train.migrate_legacy() == ""


def test_migrated_train_resumes_at_its_phase(train):
    train.STATE_DIR.mkdir(parents=True)
    (train.STATE_DIR / "state.json").write_text(json.dumps(LEGACY))
    train.migrate_legacy()
    w = train.Fake(rcpt={"version": "0.9.29", "freeze_token": "t"}, tags={"v0.9.29"})
    assert train.tick(w) == 0
    assert [c for c in w.calls if c[0] == "release"] == [("release", "0.9.29", "x")]
    assert json.loads((train.STATE_DIR / "v0.9.29.json").read_text())["phase"] == "done"


def test_migration_runs_only_under_the_train_lock_and_never_on_dry_run(train):
    src = Path(train.__file__).read_text()
    body = src[src.index("def locked("):]
    assert body.index("fcntl.LOCK_NB") < body.index("migrate_legacy()")
    assert '"--dry-run" not in flags and (moved := migrate_legacy())' in body


def test_an_idle_legacy_state_migrates_to_the_index(train):
    train.STATE_DIR.mkdir(parents=True)
    (train.STATE_DIR / "state.json").write_text(json.dumps({"phase": "idle", "last_start": 5.0, "last_version": "0.9.28"}))
    train.migrate_legacy()
    assert not list(train.STATE_DIR.glob("v*.json"))
    assert train.load_state() == {"phase": "idle", "last_start": 5.0, "last_version": "0.9.28"}


def test_release_status_reads_the_active_version_file(tool, tmp_path):
    from steuermaschine_heinzel.native.release_read_lib import status as status
    (tmp_path / "v0.9.29.json").write_text(json.dumps({"phase": "done", "version": "0.9.29"}))
    (tmp_path / "v0.9.30.json").write_text(json.dumps({"phase": "release", "version": "0.9.30"}))
    (tmp_path / "v0.9.30.log").write_text("x")
    assert status.active_train(str(tmp_path))["version"] == "0.9.30"
    (tmp_path / "state.json").write_text(json.dumps(LEGACY))
    assert status.active_train(str(tmp_path))["version"] == "0.9.29"


# ---------------------------------------------------------------- hz-release: the freeze ends at the tag

def _core(rel, monkeypatch, tagged_from_start: bool):
    calls = []
    tagged = {"v": tagged_from_start}
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.4", "patch"))
    monkeypatch.setattr(rel, "rejection_guard", lambda v: None)
    monkeypatch.setattr(rel, "has_tag", lambda repo, tag: tagged["v"])
    monkeypatch.setattr(rel, "main_candidate_gate", lambda v: True)
    monkeypatch.setattr(rel, "release_pr", lambda tag: {"state": "MERGED"})

    def fake_step(name, done, act, timeout, check=None):
        calls.append(name)
        if name == "tag":
            tagged["v"] = True
    monkeypatch.setattr(rel, "step", fake_step)
    monkeypatch.setattr(rel, "release_published", lambda tag: True)

    def after_tag(*a, **kw):
        calls.append(("frozen at pypi", rel.hzlib.merge_frozen() or rel.paths()[0].exists()
                      or (bool(rel.state().get("freeze_token")) and rel.snapshot_hold_path().exists())))
    monkeypatch.setattr(rel, "pypi_step", after_tag)
    monkeypatch.setattr(rel, "compose_step", lambda *a: None)
    monkeypatch.setattr(rel, "pinned_snapshot", lambda v: None)
    monkeypatch.setattr(rel, "acceptance_step", lambda *a: 7)
    monkeypatch.setattr(rel, "verdict", lambda run: (True, []))
    return calls


@pytest.mark.parametrize("snapshot", [True, False])
def test_release_lifts_its_freeze_right_after_the_tag(tool, home, monkeypatch, snapshot):
    rel = tool("hz-release")
    calls = _core(rel, monkeypatch, tagged_from_start=False)
    if snapshot:
        monkeypatch.setattr(rel, "acquire_snapshot_freeze", lambda v: (rel.remember(freeze_token="a" * 32),
                            rel.hzlib.release_holds().mkdir(parents=True, exist_ok=True),
                            rel.snapshot_hold_path().write_text(v)))
    else:
        rel.paths()[0].touch()
    assert rel.release_core("1.2.4", "", 1, snapshot, candidate_gate=True) == 0
    assert calls.index("tag") < calls.index(("frozen at pypi", False))
    assert rel.state()["freeze_lifted_at"] and rel.state()["finished"]
    assert not rel.paths()[0].exists()


def test_resume_past_the_tag_never_freezes_again(tool, home, monkeypatch):
    rel = tool("hz-release")
    calls = _core(rel, monkeypatch, tagged_from_start=True)
    rel.remember(version="1.2.4", freeze_token="b" * 32)
    monkeypatch.setattr(rel, "acquire_snapshot_freeze", lambda v: pytest.fail("a tagged release must not freeze main again"))
    assert rel.release_core("1.2.4", "", 1, True, candidate_gate=True) == 0
    assert "freeze" not in calls and ("frozen at pypi", False) in calls


def test_lifting_at_the_tag_keeps_an_incident_hold(tool, home, monkeypatch):
    rel = tool("hz-release")
    incident = rel.paths()[0]
    incident.touch()
    rel.remember(version="1.2.4", freeze_token="c" * 32)
    rel.hzlib.release_holds().mkdir(parents=True, exist_ok=True)
    rel.snapshot_hold_path().write_text("1.2.4")
    rel.lift_freeze_at_tag(True, incident)
    assert incident.exists(), "the shared sentinel is not the release's own hold"
    assert not rel.snapshot_hold_path().exists()


# ---------------------------------------------------------------- 2026-11-08: two trains, cut N+1 while N's tail runs

def _stage(train, version, phase, **fields):
    train.save_state({"phase": phase, "version": version, "previous": "0.9.26", "intro": "x", "tip": "abc",
                      "start": 1.0, "last_start": 1.0, "attempts": 0, "token": "t"} | fields)


def _n_tail(train, **kw):
    """N = 0.9.27 tagged and published, its tail receipt handed off; the global receipt is absent."""
    return train.Fake(**({"rcpt": {}, "tags": {"v0.9.27"}, "subjects": ["fix(web): one (#700)"]} | kw))


def test_9_n_in_its_tail_with_acceptance_dispatched_cuts_n_plus_1(train):
    _stage(train, "0.9.27", "mini")
    w = _n_tail(train, held={"v0.9.27.lock"}, tails=[("0.9.27", {"version": "0.9.27", "acceptance_requested": True})],
                release_result=(1, "STOPPED: previous release accepted: timed out. Run the same command again"))
    assert train.tick(w) == 1
    assert "skip: train v0.9.27 is running" in w.out
    assert ("release", "0.9.28", "One.") in w.calls and ("drive", "0.9.27") not in w.calls
    assert [(t["version"], t["phase"]) for t in train.active_trains()] == [("0.9.27", "mini"), ("0.9.28", "release")]


def test_10_tail_without_acceptance_dispatched_waits_in_the_merge_window(train):
    _stage(train, "0.9.27", "release")
    w = _n_tail(train, held={"v0.9.27.lock"}, tails=[("0.9.27", {"version": "0.9.27", "freeze_lifted_at": "x"})])
    assert train.tick(w) == 0 and w.out[-1] == "wait: merge window, v0.9.27 acceptance not dispatched yet"
    assert [t["version"] for t in train.active_trains()] == ["0.9.27"]


def test_10b_an_unreadable_tail_receipt_fails_closed(train):
    class Broken(train.Fake):
        def tail_receipts(self):
            raise ValueError("Expecting value")
    w = Broken(rcpt={})
    assert train.tick(w) == 0 and "unreadable" in w.out[-1] and not train.active_trains()


def test_10c_a_tagged_n_with_a_draft_release_is_never_cut_again(train):
    _stage(train, "0.9.28", "mini")  # tagged, GitHub release still draft: releases/latest is v0.9.27
    w = _n_tail(train, held={"v0.9.28.lock"}, tags={"v0.9.28"},
                tails=[("0.9.28", {"version": "0.9.28", "acceptance_requested": True})])
    assert train.tick(w) == 0 and "v0.9.28 was cut already" in w.out[-1]


def test_11_ticks_drive_trains_oldest_first_and_skip_a_running_one(train):
    _stage(train, "0.9.27", "mini")
    _stage(train, "0.9.28", "release")
    w = _n_tail(train, held={"v0.9.27.lock", "v0.9.28.lock"})
    train.tick(w)
    assert not [c for c in w.calls if c[0] == "drive"]
    assert w.out[:2] == ["skip: train v0.9.27 is running", "skip: train v0.9.28 is running"]
    w = _n_tail(train, held={"mini.lock"}, release_result=(1, "STOPPED: x"))
    train.tick(w)
    assert [c for c in w.calls if c[0] == "drive"] == [("drive", "0.9.27"), ("drive", "0.9.28")]
    w = _n_tail(train, held={"v0.9.28.lock"})
    assert train.run_version(w, "0.9.28") == 0 and w.out[-1] == "skip: train v0.9.28 is running"


def test_11b_the_tick_never_holds_the_train_lock_while_a_train_runs(train):
    src = Path(train.__file__).read_text()
    drive = src[src.index("    def drive(self"):src.index("    def latest_tag")]
    assert "start_new_session=True" in drive and "edge.train_run(version)" in drive
    runner = src[src.index("def run_version("):src.index("def resume(")]
    assert 'w.lock(f"v{version}.lock")' in runner and "train.lock" not in runner


def test_12_n_not_accepted_makes_n_plus_1_wait_without_an_attempt(train):
    _stage(train, "0.9.27", "blocked", reason="published but NOT ACCEPTED (run 42)")
    _stage(train, "0.9.28", "release")
    text = ("STOPPED: previous release 0.9.27 NOT ACCEPTED (run 42): fix forward and hz-release --absorb 0.9.27 "
            "--reason ..., or hz-train clear. Run the same command again")
    w = _n_tail(train, release_result=(1, text))
    assert train.tick(w) == 3, "N stays blocked"
    n1 = json.loads((train.STATE_DIR / "v0.9.28.json").read_text())
    assert n1["phase"] == "release" and n1["attempts"] == 0, "N+1 is never blocked or charged an attempt"
    assert "wait: previous release 0.9.27 NOT ACCEPTED" in w.out[-1]
    train.tick(w)
    assert len([n for n in w.notes if "NOT ACCEPTED" in n]) == 1, "noted once"
    assert json.loads((train.STATE_DIR / "v0.9.27.json").read_text())["phase"] == "blocked"


def test_12b_a_blocked_train_stops_new_cuts(train):
    _stage(train, "0.9.27", "blocked", reason="x")
    w = _n_tail(train)
    assert train.tick(w) == 3 and not [c for c in w.calls if c[0] == "release"]


def test_13_one_mini_owner_and_a_superseded_version_counts_as_live(train):
    _stage(train, "0.9.31", "mini")
    w = _n_tail(train, held={"mini.lock"}, live=True)
    assert train.run_version(w, "0.9.31") == 0 and "wait: another train is verifying the Mini" in w.out[-1]
    assert json.loads((train.STATE_DIR / "v0.9.31.json").read_text())["phase"] == "mini"
    w = _n_tail(train, live=True, live_line="LIVE-OK 0.9.32")
    assert train.run_version(w, "0.9.31") == 0
    assert "0.9.32 is accepted and live" in w.notified[0]
    assert json.loads((train.STATE_DIR / "v0.9.31.json").read_text())["phase"] == "done"
    assert train.live_ok("LIVE-OK 0.9.32", "0.9.31") and train.live_ok("LIVE-OK 0.9.31", "0.9.31")
    assert not train.live_ok("LIVE-OK 0.9.32+dirty", "0.9.31")
    assert not train.live_ok("LIVE-OK 0.9.32.1", "0.9.31")
    assert not train.live_ok("LIVE-OK 0.9.30", "0.9.31") and not train.live_ok("NOT-LIVE 0.9.32", "0.9.31")


def test_14_every_defaults_to_zero_and_hz_train_every_restores_the_floor(tool, tmp_path, monkeypatch):
    monkeypatch.delenv("HZ_TRAIN_EVERY", raising=False)
    assert tool("hz-train").EVERY == 0
    monkeypatch.setenv("HZ_TRAIN_EVERY", "60")
    mod = tool("hz-train")
    monkeypatch.setattr(mod, "STATE_DIR", tmp_path / "train")
    monkeypatch.setattr(mod, "AUTO_CUT", True)
    w = mod.Fake(tag_time=10_000_000.0 - 20 * 60)
    assert mod.tick(w) == 0 and "next train in 40 min" in w.out[-1]


def test_clear_marks_the_named_or_newest_train_only(train):
    _stage(train, "0.9.27", "blocked", reason="x")
    _stage(train, "0.9.28", "release")
    train.clear()
    assert json.loads((train.STATE_DIR / "v0.9.28.json").read_text())["phase"] == "cleared"
    assert json.loads((train.STATE_DIR / "v0.9.27.json").read_text())["phase"] == "blocked"
    train.clear("0.9.27")
    assert not train.active_trains()
    assert train.main(["run"]) == 2 and train.main(["clear", "x"]) == 2 and train.main(["run", "0.9.28", "y"]) == 2


def test_status_lists_trains_and_tails(train):
    _stage(train, "0.9.27", "mini")
    w = _n_tail(train, tails=[("0.9.27", {"finished": True, "accepted": True, "acceptance_run": 42}),
                              ("0.9.26", {"finished": True, "accepted": False, "absorbed_by": "0.9.27"})])
    out = train.status(w)
    assert "train v0.9.27: mini (idle, attempts 0)" in out and "0.9.27 tail: ACCEPTED, acceptance run 42" in out
    assert "0.9.26 tail: NOT ACCEPTED (absorbed by 0.9.27)" in out


def test_finish_of_n_leaves_deferred_ci_to_the_running_candidate(train):
    _stage(train, "0.9.27", "finish")
    _stage(train, "0.9.28", "release")
    w = _n_tail(train)
    assert train.run_version(w, "0.9.27") == 0
    assert ("defer_resume",) not in w.calls and "left for train v0.9.28" in w.notes[-1]


def test_r2_clear_is_refused_while_the_runner_holds_its_lock(train):
    import fcntl
    _stage(train, "0.9.28", "release")
    with (train.STATE_DIR / "v0.9.28.lock").open("a") as runner:
        fcntl.flock(runner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert train.clear("0.9.28").startswith("refused: train v0.9.28 is running")
        assert train.clear().startswith("refused:")
    assert json.loads((train.STATE_DIR / "v0.9.28.json").read_text())["phase"] == "release"
    assert train.clear("0.9.28").startswith("train state cleared")
    assert train.clear("0.9.99").startswith("refused: no active train")


@pytest.fixture
def incident(train, home, monkeypatch):
    freeze = statedir.root() / "merge-freeze"
    gate = statedir.root() / "gate.hold"
    gate.write_bytes(b"another gate owner\n")
    release = statedir.root() / "release.json"
    release.write_bytes(b'{"version": "0.9.33", "finished": true, "accepted": true, "acceptance_run": 42}\n')
    monkeypatch.setattr(train, "GATE_HOLD", gate)
    monkeypatch.setattr(train, "RECEIPT", release)
    return freeze, gate, release


def test_incident_blocks_dispatch_before_legacy_migration_and_direct_runner(train, incident, monkeypatch):
    freeze, gate, release = incident
    # A dangling sentinel must still hold, including for a direct runner that was already dispatched.
    freeze.symlink_to("missing")
    train.STATE_DIR.mkdir()
    legacy = train.STATE_DIR / "state.json"
    legacy.write_text(json.dumps(LEGACY | {"phase": "mini"}))
    before = {path: path.read_bytes() for path in (legacy, gate, release)}

    def unexpected(*args, **kwargs):
        pytest.fail("incident hold allowed a release, Mini, notification, or CI command")

    monkeypatch.setattr(train.subprocess, "run", unexpected)
    monkeypatch.setattr(train.subprocess, "Popen", unexpected)
    assert train.locked("tick", set()) == 0
    assert train.run_version(train.Real(), LEGACY["version"]) == 0
    assert freeze.is_symlink() and not (train.STATE_DIR / "state.json.migrated").exists()
    assert all(path.read_bytes() == original for path, original in before.items())


@pytest.mark.parametrize("when", ["mini_lock", "verified"])
def test_late_incident_preserves_mini_train(train, incident, monkeypatch, when):
    from contextlib import contextmanager

    freeze, gate, release = incident
    version = "0.9.33"
    _stage(train, version, "mini", attempts=2)
    state = train.version_path(version)
    before = {path: path.read_bytes() for path in (state, gate, release)}
    w = train.Fake()
    w.incident_active = train.Real().incident_active

    @contextmanager
    def lock(name):
        if name == "mini.lock" and when == "mini_lock":
            freeze.write_text("incident")
        yield True

    def verify(_):
        assert when == "verified", "incident must be checked after acquiring the Mini lock"
        freeze.write_text("incident")
        return "LIVE-OK 0.9.33"

    monkeypatch.setattr(w, "lock", lock)
    monkeypatch.setattr(w, "verify_mini", verify)
    assert train.run_version(w, version) == 0
    assert not w.calls and not w.notified
    assert all(path.read_bytes() == original for path, original in before.items())
    assert freeze.read_text() == "incident"


def test_release_owned_hold_allows_existing_candidate_but_blocks_new_cut(train, incident):
    freeze, _, _ = incident
    holds = freeze.with_name("release-holds")
    holds.mkdir()
    (holds / "candidate").write_text("0.9.33")
    w = train.Fake(release_result=(1, "STOPPED: candidate pending"))
    w.incident_active = train.Real().incident_active
    w.freeze_active = train.Real().freeze_active
    assert train.tick(w) == 0 and not w.calls
    train.save_state(LEGACY | {"attempts": 0})
    assert train.tick(w) == 1
    assert ("release", LEGACY["version"], LEGACY["intro"]) in w.calls
    assert ("defer_cancel",) in w.calls


@pytest.mark.parametrize("phase", ["mini", "finish"])
def test_mini_requires_successful_fresh_proof_before_notify_and_ci(train, incident, monkeypatch, phase):
    import subprocess

    version = "0.9.33"
    _stage(train, version, phase, attempts=2)
    state = train.version_path(version)
    _, gate, release = incident
    before = {path: path.read_bytes() for path in (state, gate, release)}
    w = train.Fake()
    w.verify_mini = train.Real().verify_mini
    w.incident_active = train.Real().incident_active

    def verifier(argv, **kwargs):
        assert argv == [*train.edge.sm(), "mini", "update", "--minimum", version]
        assert kwargs["timeout"] > 0
        return subprocess.CompletedProcess(argv, 1, "LIVE-OK 0.9.34\n", "")

    monkeypatch.setattr(train.subprocess, "run", verifier)
    assert train.run_version(w, version) == 0
    assert not w.calls and not w.notified and "verifier exited 1" in w.out[-1]
    assert all(path.read_bytes() == original for path, original in before.items())

    monkeypatch.setattr(train.subprocess, "run",
                        lambda argv, **_: subprocess.CompletedProcess(argv, 0, "LIVE-OK 0.9.35\n", ""))
    assert train.run_version(w, version) == 0
    assert train.run_version(w, version) == 0
    assert w.calls == [("defer_resume",)]
    assert len(w.notified) == (1 if phase == "mini" else 0)
    assert json.loads(state.read_text())["live_version"] == "0.9.35"


def test_incident_after_notification_preserves_finish_and_defers_ci(train, incident, monkeypatch):
    freeze, _, _ = incident
    version = "0.9.33"
    _stage(train, version, "mini")
    w = train.Fake()
    w.incident_active = train.Real().incident_active

    def notify(text):
        w.notified.append(text)
        freeze.write_text("incident")

    monkeypatch.setattr(w, "notify", notify)
    assert train.run_version(w, version) == 0
    assert len(w.notified) == 1 and not w.calls
    assert json.loads(train.version_path(version).read_text())["phase"] == "finish"
    freeze.unlink()
    w.live = False
    assert train.run_version(w, version) == 0
    assert not w.calls and len(w.notified) == 1


def test_incident_while_listing_deferred_ci_preserves_finish(train, incident, monkeypatch):
    import subprocess

    freeze, _, _ = incident
    version = "0.9.33"
    _stage(train, version, "finish")
    state = train.version_path(version)
    before = state.read_bytes()
    w = train.Fake()
    w.defer_resume = train.Real().defer_resume

    def command(argv, **kwargs):
        assert argv == [*train.edge.sm(), "ci", "deferred"]
        freeze.write_text("incident")
        return subprocess.CompletedProcess(argv, 0, "deferred 123\n", "")

    monkeypatch.setattr(train.subprocess, "run", command)
    assert train.run_version(w, version) == 0
    assert state.read_bytes() == before and not w.notified


def test_auto_cut_off_drives_only_and_now_cuts(train, monkeypatch):
    monkeypatch.setattr(train, "AUTO_CUT", False)
    w = train.Fake()
    assert train.tick(w) == 0 and not w.calls and "auto-cut is off" in w.out[-1] and "sm daemon train --now" in w.out[-1]
    assert train.tick(w, now_flag=True) == 0 and ("release", "0.9.28", "Thin scrollbars; Clear button.") in w.calls


def test_accepted_but_not_live_waits_in_the_mini_phase_without_an_attempt(train):
    # `sm release resume X --live` exits 1 when the Mini is not live yet; the verdict in the tail receipt decides.
    w = train.Fake(release_result=(1, "ACCEPTED 0.9.28 (run 42)\nNOT-LIVE: the version served on port 8737 is 0.9.27, the installed one 0.9.28"),
                   live=False, tails=[("0.9.28", {"version": "0.9.28", "finished": True, "accepted": True, "acceptance_run": 42})])
    assert train.tick(w) == 0 and "needs an accepted live build" in w.out[-1]
    st = json.loads(train.version_path("0.9.28").read_text())
    assert st["phase"] == "mini" and st["attempts"] == 0 and not w.notified
    w.live = True
    assert train.tick(w) == 0 and len(w.notified) == 1
    assert json.loads(train.version_path("0.9.28").read_text())["phase"] == "done"


def test_a_failed_release_without_the_verdict_still_counts_an_attempt(train):
    w = train.Fake(release_result=(1, "STOPPED: compose failed"))
    assert train.tick(w) == 1 and json.loads(train.version_path("0.9.28").read_text())["attempts"] == 1

    class Unreadable(train.Fake):
        def tail_receipts(self):
            raise ValueError("broken")
    assert not train.accepted(Unreadable(), "0.9.28")
