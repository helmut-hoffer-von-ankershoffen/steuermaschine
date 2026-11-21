"""`sm daemon train`, `sm release train status | next | clear | plan | resume-ci` through the command tree.

fixtures/release/train/ and fixtures/release/tails/: real state files of the release train and real tail receipts,
copied 2026-11-12 with `cp` (read only); on 2026-11-13 the three train files were still byte-identical to the live
ones. The tests run on copies in tmp_path. GitHub is `steuermaschine.github.testing.FakeGh`; nothing is started."""

import json
import shutil
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import train as area
from steuermaschine_heinzel.plugin import plugin

REAL = Path(__file__).parent / "fixtures" / "release"
OPEN = [{"number": 10, "headRefName": "combine/a"}, {"number": 11, "headRefName": "fix/q"}, {"number": 12, "headRefName": "feat/other"}]
QUEUE = {"entries": [{"pr": "11", "status": "queued"}, {"pr": "12", "status": "needs work"}]}


def make(tmp_path, monkeypatch, gh, reviewed=True):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / ".cache/flow"))
    monkeypatch.delenv("HZ_RELEASE_TAG_ON_MAIN", raising=False)
    (tmp_path / ".cache").mkdir(exist_ok=True)
    (tmp_path / "records").mkdir(exist_ok=True)
    (tmp_path / "records/AGENTS.md").write_text("## Log\n")
    shutil.copytree(REAL / "train", tmp_path / "train", dirs_exist_ok=True)
    shutil.copytree(REAL / "tails", statedir.root() / "release", dirs_exist_ok=True)
    conf = {"release_train_state_dir": str(tmp_path / "train"), "release_train_checkout": str(tmp_path / "checkout"), "queue_records_dir": str(tmp_path / "records")}
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": conf}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed), stack, settings, _providers=fake_providers(commands=gh))


@pytest.fixture
def gh():
    return FakeGh()


@pytest.fixture
def rt(tmp_path, monkeypatch, gh):
    runtime = make(tmp_path, monkeypatch, gh)
    runtime.home = tmp_path
    return runtime


def doc(rt, model, *args):
    out = invoke(rt, ["--format", "json", *args])
    return out, model.model_validate(json.loads(out.stdout))


# ----------------------------------------------------------------------------- status
def test_status_reads_the_real_train_files_and_tail_receipts(rt, gh):
    out, status = doc(rt, area.TrainStatus, "release", "train", "status")
    assert out.exit_code == 0 and status.trains == [] and status.candidate == ""  # 0.9.35 is done, 0.9.36 is cleared
    assert len(status.tails) == 2 and status.tails[0].startswith("0.9.38 tail: ") and status.tails[1] == "0.9.39 tail: ACCEPTED, acceptance run 43158281029"
    assert status.state == json.loads((REAL / "train" / "train.json").read_text()) | {"phase": "idle"}  # no active train: the cadence file
    assert gh.calls == [] and "hz-" not in out.stdout


def test_status_names_an_active_train_and_a_candidate_receipt(rt):
    blocked = json.loads((REAL / "train" / "v0.9.36.json").read_text()) | {"phase": "blocked"}
    (rt.home / "train" / "v0.9.36.json").write_text(json.dumps(blocked))
    (statedir.root() / "release.json").write_text((REAL / "receipt-0.9.40-pretag.json").read_text())
    _out, status = doc(rt, area.TrainStatus, "release", "train", "status")
    assert [(t.version, t.phase, t.running, t.attempts) for t in status.trains] == [("0.9.36", "blocked", False, 2)]
    assert status.trains[0].reason.startswith("published but NOT ACCEPTED (run 42918110552)") and status.candidate == "0.9.40" and status.state["version"] == "0.9.36"
    text = invoke(rt, ["release", "train", "status"]).stdout
    assert text.splitlines()[0].startswith("train v0.9.36: blocked (idle, attempts 2): published but NOT ACCEPTED (run 42918110552); see the train's v0.9.36.log")
    assert "0.9.40 candidate: release receipt before its handoff" in text and "hz-" not in text


# ----------------------------------------------------------------------------- clear
def quiet(gh):
    """GitHub as after a finished or dropped candidate: no open PR, no run of a release or bump branch."""
    gh.on("pr list", [])
    gh.on("run list", [])


def test_clear_marks_the_blocked_train_cleared_and_writes_a_note(rt, gh):
    quiet(gh)
    blocked = json.loads((REAL / "train" / "v0.9.36.json").read_text()) | {"phase": "blocked"}
    (rt.home / "train" / "v0.9.36.json").write_text(json.dumps(blocked))
    out, cleared = doc(rt, area.TrainCleared, "release", "train", "clear", "0.9.36")
    assert out.exit_code == 0 and cleared.cleared and cleared.version == "0.9.36" and cleared.lines[0].startswith("train state cleared (was blocked v0.9.36:")
    after = json.loads((rt.home / "train" / "v0.9.36.json").read_text())
    assert after["phase"] == "cleared" and {k: v for k, v in after.items() if k not in ("phase", "cleared_at")} == {k: v for k, v in blocked.items() if k not in ("phase", "cleared_at")}
    note = (rt.home / "records/AGENTS.md").read_text().splitlines()[-1]
    assert " release train: train state cleared (was blocked v0.9.36:" in note and "sm release train" not in note.split("see ")[0]
    assert gh.writes() == [] and {" ".join(c[1:3]) for c in gh.calls} == {"pr list", "run list"}
    out = invoke(rt, ["release", "train", "clear", "0.9.99"])
    assert out.exit_code == 1 and out.stdout.strip() == "refused: no active train v0.9.99"
    assert invoke(rt, ["release", "train", "clear", "next"]).exit_code == 2


def test_clear_is_refused_while_the_trains_runner_holds_its_lock(rt, gh):
    import fcntl

    quiet(gh)
    (rt.home / "train" / "v0.9.36.json").write_text(json.dumps(json.loads((REAL / "train" / "v0.9.36.json").read_text()) | {"phase": "release"}))
    with (rt.home / "train" / "v0.9.36.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = invoke(rt, ["release", "train", "clear", "0.9.36"])
    assert out.exit_code == 1 and out.stdout.strip() == "refused: train v0.9.36 is running: sm release resume 0.9.36 --pause first, then clear"
    assert json.loads((rt.home / "train" / "v0.9.36.json").read_text())["phase"] == "release"


def pipelined(rt):
    """The ordinary pipelined state, built from copies of the real files: N (0.9.39) blocked by NOT ACCEPTED, N+1
    (0.9.40) cut and waiting in phase release with its runner gone, the real pre-tag receipt of 0.9.40 in place."""
    real = json.loads((REAL / "train" / "v0.9.36.json").read_text())
    for version, phase in (("0.9.39", "blocked"), ("0.9.40", "release")):
        data = real | {"version": version, "previous": f"0.9.{int(version[-2:]) - 1}", "phase": phase,
                       "reason": f"published but NOT ACCEPTED (run 42918110552); see the train's v{version}.log"}
        data.pop("cleared_at")
        (rt.home / "train" / f"v{version}.json").write_text(json.dumps(data, indent=1, sort_keys=True))
    (statedir.root() / "release.json").write_text((REAL / "receipt-0.9.40-pretag.json").read_text())
    return {p.name: p.read_bytes() for p in (rt.home / "train").iterdir()}


def files(rt):
    return {p.name: p.read_bytes() for p in (rt.home / "train").iterdir() if p.name != "train.lock"}


def test_clear_takes_no_default_version(rt, gh):
    before = pipelined(rt)
    out = invoke(rt, ["release", "train", "clear"])
    assert out.exit_code == 2 and files(rt) == before and gh.calls == []  # the old default cleared the newest train, 0.9.40


def test_clear_refuses_the_waiting_candidate_while_its_receipt_is_unfinished(rt, gh):
    before = pipelined(rt)
    out, cleared = doc(rt, area.TrainCleared, "release", "train", "clear", "0.9.40")
    assert out.exit_code == 1 and not cleared.cleared and cleared.version == "0.9.40"
    assert cleared.lines == ["refused: train v0.9.40 is not cleared: the release receipt shows 0.9.40 unfinished: "
                             "sm release drop 0.9.40 --reason ... (--close-pr when its PR is open), then clear"]
    assert files(rt) == before and gh.calls == [] and (rt.home / "records/AGENTS.md").read_text() == "## Log\n"


def test_clear_refuses_the_candidate_while_its_release_pr_is_open(rt, gh):
    before = pipelined(rt)
    (statedir.root() / "release.json").unlink()  # even without the receipt: the PR is still the train's
    gh.on("pr list", [{"number": 812, "headRefName": "release/v0.9.40"}, {"number": 9, "headRefName": "release/v0.9.39"}])
    gh.on("run list", [])
    out = invoke(rt, ["release", "train", "clear", "0.9.40"])
    assert out.exit_code == 1 and out.stdout.strip().endswith("the release PR of 0.9.40 is open (#812): sm release drop 0.9.40 --reason ... "
                                                               "(--close-pr when its PR is open), then clear")
    assert files(rt) == before and gh.writes() == []


def test_clear_refuses_the_candidate_while_its_run_is_active(rt, gh):
    before = pipelined(rt)
    (statedir.root() / "release.json").unlink()
    gh.on("pr list", [])
    gh.on("run list", lambda argv: [{"status": "in_progress", "databaseId": 77}] if "bump/v0.9.40" in argv else [{"status": "completed", "databaseId": 76}])
    out = invoke(rt, ["release", "train", "clear", "0.9.40"])
    assert out.exit_code == 1 and "the candidate run of 0.9.40 is still active (bump/v0.9.40: run 77)" in out.stdout
    assert files(rt) == before and gh.writes() == []


def test_clear_refuses_when_github_cannot_be_read(rt, gh):
    before = pipelined(rt)
    (statedir.root() / "release.json").unlink()
    gh.on("pr list", 502)
    out = invoke(rt, ["release", "train", "clear", "0.9.40"])
    assert out.exit_code == 1 and "cannot read the open PRs" in out.stdout and files(rt) == before
    gh.on("pr list", [])
    gh.on("run list", 502)
    out = invoke(rt, ["release", "train", "clear", "0.9.40"])
    assert out.exit_code == 1 and "cannot read the runs of release/v0.9.40" in out.stdout and files(rt) == before
    (statedir.root() / "release.json").write_text("{not json")
    out = invoke(rt, ["release", "train", "clear", "0.9.40"])
    assert out.exit_code == 1 and "the release receipt is unreadable" in out.stdout and files(rt) == before


def test_clear_of_the_blocked_train_leaves_the_waiting_candidate_alone(rt, gh):
    """The allowed path in the pipelined state: N is cleared, N+1's file, the receipt and GitHub stay as they are."""
    before = pipelined(rt)
    receipt = (statedir.root() / "release.json").read_bytes()
    quiet(gh)
    out, cleared = doc(rt, area.TrainCleared, "release", "train", "clear", "0.9.39")
    assert out.exit_code == 0 and cleared.cleared and cleared.version == "0.9.39" and cleared.lines[0].startswith("train state cleared (was blocked v0.9.39:")
    after = files(rt)
    assert after["v0.9.40.json"] == before["v0.9.40.json"] and json.loads(after["v0.9.39.json"])["phase"] == "cleared"
    assert (statedir.root() / "release.json").read_bytes() == receipt and gh.writes() == []
    note = (rt.home / "records/AGENTS.md").read_text().splitlines()[-1]
    assert "train state cleared (was blocked v0.9.39:" in note


def test_clear_after_the_candidate_was_dropped(rt, gh):
    """The old tool's other use: `sm release drop X` archived the receipt and closed the PR, no run is left; clear X."""
    before = pipelined(rt)
    (statedir.root() / "release.json").unlink()
    gh.on("pr list", [{"number": 5, "headRefName": "fix/other"}])
    gh.on("run list", [{"status": "completed", "databaseId": 76}])
    out, cleared = doc(rt, area.TrainCleared, "release", "train", "clear", "0.9.40")
    assert out.exit_code == 0 and cleared.cleared and cleared.lines[0].startswith("train state cleared (was release v0.9.40:")
    after = files(rt)
    assert after["v0.9.39.json"] == before["v0.9.39.json"] and json.loads(after["v0.9.40.json"])["phase"] == "cleared"
    finished = json.loads((REAL / "receipt-0.9.40-pretag.json").read_text()) | {"finished": True}
    (statedir.root() / "release.json").write_text(json.dumps(finished))  # a finished receipt of that version does not refuse
    assert invoke(rt, ["release", "train", "clear", "0.9.39"]).exit_code == 0


# ----------------------------------------------------------------------------- the tick
def test_a_dry_tick_with_an_open_release_pr_starts_nothing_and_says_why(rt, gh, monkeypatch):
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("a dry tick started a process"))
    gh.on("pr list", [{"headRefName": "release/v0.9.41"}])
    before = {p.name: p.read_bytes() for p in (rt.home / "train").iterdir()}
    out, tick = doc(rt, area.TrainTick, "daemon", "train", "--dry-run")
    assert out.exit_code == 0 and tick.action == "tick" and tick.lines[0].startswith("sm release train 20")
    assert any(line.startswith("skip:") for line in tick.lines), tick.lines
    assert {p.name: p.read_bytes() for p in (rt.home / "train").iterdir() if p.name != "train.lock"} == before and gh.writes() == []
    assert "hz-" not in out.stdout


def test_the_runner_option_takes_one_exact_version(rt):
    for args in (["daemon", "train", "--run", "patch"], ["daemon", "train", "--run", "0.9.41", "--dry-run"]):
        assert invoke(rt, args).exit_code == 2, args
    out, tick = doc(rt, area.TrainTick, "daemon", "train", "--run", "0.9.35")  # a finished train: its runner has nothing to do
    assert out.exit_code == 0 and tick.action == "run" and tick.version == "0.9.35" and tick.lines[0].startswith("sm daemon train 0.9.35 20")


def test_the_train_starts_one_release_as_sm_release_resume_and_its_runner_as_sm_daemon_train(rt):
    from steuermaschine.native import NativeRuntime, using

    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        module = area.train()
    assert (rt.home / "train", rt.home / "checkout") == (module.STATE_DIR, module.CHECKOUT)
    sm = module.edge.sm()
    assert module.edge.train_run("0.9.41") == [*sm, "daemon", "train", "--run", "0.9.41"]
    env = module.Real().release_env()
    assert env["SM_HEINZEL__RELEASE_DIR"] == str(rt.home / "checkout") == env["HZ_RELEASE_DIR"]
    assert "HZ_GATE_PRIORITY" not in env and "HZ_GATE_HOLD_EXEMPT" not in env  # the run lock reads no environment
    assert module.edge.gate() == [*sm, "gate", "run", "--priority", "2", "--hold-exempt", "--"]  # the options instead
    source = Path(module.__file__).read_text()
    assert 'cmd = [*edge.sm(), "release", "resume", version, "--intro", intro, "--snapshot-acceptance", "--candidate-gate", "--live"]' in source
    assert 'with_name("hz-' not in source and '["hz-' not in source  # no path to, and no argument list of, an old tool


# ----------------------------------------------------------------------------- resume-ci (the two tests of the old shell tool, on the port)
class Listed:
    """The list of deferred runs in the store, read and written as lines `id branch workflow` like `sm ci deferred`."""

    def __init__(self, store):
        self.store = store

    def write_text(self, text):
        rows = [dict(zip(("id", "branch", "workflow"), line.split(None, 2), strict=False)) for line in text.splitlines()]
        self.store.save(area.DEFERRED, "runs", {"runs": rows})

    def read_text(self):
        return "".join(f"{r['id']} {r['branch']} {r['workflow']}\n" for r in self.store.load(area.DEFERRED, "runs")["runs"])


def deferred(rt, gh, rerun_fails=()):
    log = Listed(rt.providers.store)
    log.write_text("1 combine/a CI\n2 fix/q CI\n3 feat/other CI\n4 gone/pr CI\n")
    gh.on("pr list", OPEN)
    gh.on("run list", [])
    gh.on("queue list", QUEUE)
    gh.on("run rerun", lambda argv: 1 if argv[3] in rerun_fails else "")
    return log


def test_resume_ci_reruns_combine_and_queued_only(rt, gh):
    log = deferred(rt, gh)
    out, done = doc(rt, area.ResumedCI, "release", "train", "resume-ci")
    assert out.exit_code == 0 and done.rerun == ["1", "2"] and done.dropped == ["4"] and done.later == ["3"] and done.failed == []
    assert [call[3] for call in gh.calls if call[1:3] == ["run", "rerun"]] == ["1", "2"]
    assert log.read_text().splitlines() == ["3 feat/other CI"]
    assert "drop 4 gone/pr" in "\n".join(done.lines)


def test_resume_ci_dry_run_changes_nothing(rt, gh):
    log = deferred(rt, gh)
    out, done = doc(rt, area.ResumedCI, "release", "train", "resume-ci", "--dry-run")
    assert out.exit_code == 0 and not any(call[1:3] == ["run", "rerun"] for call in gh.calls) and len(log.read_text().splitlines()) == 4
    assert "would rerun 1 #10" in "\n".join(done.lines) and done.rerun == ["1", "2"]


def test_resume_ci_keeps_a_failed_rerun_refuses_during_a_release_run_and_says_when_nothing_is_deferred(rt, gh):
    log = deferred(rt, gh, rerun_fails=("2",))
    out, done = doc(rt, area.ResumedCI, "release", "train", "resume-ci")
    assert done.failed == ["2"] and done.rerun == ["1"] and log.read_text().splitlines() == ["2 fix/q CI", "3 feat/other CI"]
    gh.on("run list", [{"headBranch": "release/v0.9.41"}])
    out = invoke(rt, ["release", "train", "resume-ci"])
    assert out.exit_code == 1 and out.stdout.strip() == "refusing: a release/* run is active (use --force)"
    assert invoke(rt, ["release", "train", "resume-ci", "--force"]).exit_code == 0
    log.write_text("")
    assert invoke(rt, ["release", "train", "resume-ci"]).stdout.strip() == "nothing deferred"


def test_resume_ci_refuses_and_keeps_the_list_when_github_cannot_be_read(rt, gh):
    log = deferred(rt, gh)
    before = log.read_text()
    gh.on("pr list", 502)  # the old tool read this as "every PR closed" and emptied the list
    out, done = doc(rt, area.ResumedCI, "release", "train", "resume-ci")
    assert out.exit_code == 1 and done.lines[0].startswith("refusing: cannot read the open PRs") and log.read_text() == before
    gh.on("pr list", OPEN)
    gh.on("run list", "not json")  # the old tool read this as "no release run active"
    out = invoke(rt, ["release", "train", "resume-ci"])
    assert out.exit_code == 1 and out.stdout.startswith("refusing: cannot read the in_progress runs") and log.read_text() == before
    assert not any(call[1:3] == ["run", "rerun"] for call in gh.calls)


def test_resume_ci_waits_while_another_command_changes_the_list(rt, gh):
    log = deferred(rt, gh)
    with rt.providers.store.lock(area.DEFERRED):
        out = invoke(rt, ["release", "train", "resume-ci"])
    assert out.exit_code == 75 and len(log.read_text().splitlines()) == 4


def test_the_former_deferred_list_is_read_only_and_tolerant(tmp_path):
    from steuermaschine_heinzel.native.train_lib import legacy

    old = tmp_path / "deferred.txt"
    old.write_text("11 combine/a CI\nnot-a-run x\n12 fix/q\n")
    assert legacy.deferred_rows(old) == [{"id": "11", "branch": "combine/a", "workflow": "CI"}, {"id": "12", "branch": "fix/q", "workflow": ""}]
    assert legacy.deferred_rows(tmp_path / "missing") == [] and legacy.deferred_rows(None) == []


# ----------------------------------------------------------------------------- gates
def test_status_and_plan_only_read(tmp_path, monkeypatch):
    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh, reviewed=False)
    assert invoke(rt, ["release", "train", "status"]).exit_code == 0 and gh.calls == []
    assert invoke(rt, ["release", "train", "plan", "1"]).exit_code == 0


def test_the_version_probe_runs_under_the_run_lock_with_priority_and_hold_exemption(rt, monkeypatch):
    import subprocess

    from steuermaschine.native import NativeRuntime, using

    (rt.home / "checkout" / ".git").mkdir(parents=True)
    calls = []

    def run(argv, *args, **kwargs):
        calls.append((list(argv), kwargs.get("env") or {}))
        return subprocess.CompletedProcess(argv, 0, "0.9.40\n" if "bump-my-version" in argv else "", "")

    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        module = area.train()
        monkeypatch.setattr(module.edge, "run", run)
        module.Real().prepare_checkout("0.9.40", "0.9.41")
    probe, env = next(call for call in calls if "bump-my-version" in call[0])
    sm = module.edge.sm()
    assert probe == [*sm, "gate", "run", "--priority", "2", "--hold-exempt", "--", "uv", "run", "--frozen", "bump-my-version", "show", "current_version"]
    assert env["SM_HEINZEL__RELEASE_DIR"] == str(rt.home / "checkout") and "HZ_GATE_PRIORITY" not in env


# ----------------------------------------------------------------------------- settings win over the old variables
def test_a_setting_wins_over_the_old_variable_and_the_tick_says_so(rt, gh, monkeypatch):
    gh.on("pr list", [{"headRefName": "release/v0.9.41"}])
    monkeypatch.setenv("HZ_TRAIN_CHECKOUT", "/elsewhere")
    monkeypatch.setenv("HZ_RELEASE_DIR", "/private/tmp/other-release")
    monkeypatch.setenv("HZ_TRAIN_EVERY", "120")
    out = invoke(rt, ["daemon", "train", "--dry-run"])
    assert out.exit_code == 0
    assert f"warning: HZ_TRAIN_CHECKOUT=/elsewhere is ignored; the setting heinzel.release_train_checkout ({rt.home / 'checkout'}) wins" in out.stderr
    assert "warning: HZ_RELEASE_DIR=/private/tmp/other-release is ignored; a release of the train is prepared in the train's checkout" in out.stderr
    assert "HZ_TRAIN_EVERY" not in out.stderr  # no setting for the cadence here: the variable applies, as before
    from steuermaschine.native import NativeRuntime, using

    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        module = area.train()
    assert (rt.home / "checkout", 120) == (module.CHECKOUT, module.EVERY)
    assert module.Real().release_env()["SM_HEINZEL__RELEASE_DIR"] == str(rt.home / "checkout")


def test_no_warning_when_the_variables_agree_or_are_unset(rt, gh, monkeypatch):
    gh.on("pr list", [{"headRefName": "release/v0.9.41"}])
    monkeypatch.setenv("HZ_RELEASE_DIR", str(rt.home / "checkout"))
    monkeypatch.setenv("HZ_TRAIN_STATE_DIR", str(rt.home / "train"))
    assert "warning" not in invoke(rt, ["daemon", "train", "--dry-run"]).stderr


# ----------------------------------------------------------------------------- the live state files, adopted as they are
def test_the_live_state_files_are_read_and_written_in_their_own_format(rt, gh):
    """Copies of the live train files: status reads them, clear rewrites one with the writer of the old train (same
    keys, indent 1, sorted keys), the cadence file and the other version's file stay byte for byte."""
    for name in ("train.json", "v0.9.35.json", "v0.9.36.json"):
        assert (rt.home / "train" / name).read_bytes() == (REAL / "train" / name).read_bytes()
    blocked = json.loads((REAL / "train" / "v0.9.36.json").read_text()) | {"phase": "blocked"}
    (rt.home / "train" / "v0.9.36.json").write_text(json.dumps(blocked, indent=1, sort_keys=True))
    quiet(gh)
    assert invoke(rt, ["release", "train", "clear", "0.9.36"]).exit_code == 0
    after = (rt.home / "train" / "v0.9.36.json").read_text()
    data = json.loads(after)
    assert after == json.dumps(data, indent=1, sort_keys=True) and set(data) == set(blocked) | {"cleared_at"} and data["phase"] == "cleared"
    for name in ("train.json", "v0.9.35.json"):
        assert (rt.home / "train" / name).read_bytes() == (REAL / "train" / name).read_bytes()
    _out, status = doc(rt, area.TrainStatus, "release", "train", "status")
    assert status.trains == [] and status.state == json.loads((REAL / "train" / "train.json").read_text()) | {"phase": "idle"}


# ----------------------------------------------------------------------------- next and plan
MERGED = [{"number": 1, "title": "chore(release): v0.9.36", "mergedAt": "2026-11-11T03:00:00Z"},
          {"number": 2, "title": "fix: after", "mergedAt": "2026-11-11T04:00:00Z"},
          {"number": 3, "title": "fix: before", "mergedAt": "2026-11-11T02:00:00Z"}]
GREEN = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"}], "mergeStateStatus": "CLEAN"}


def batch(rt, gh):
    (rt.home / "train" / "v0.9.36.json").write_text(json.dumps(json.loads((REAL / "train" / "v0.9.36.json").read_text()) | {"phase": "release"}))
    gh.on("pr list", MERGED)
    gh.on("pr view", GREEN)
    gh.on("queue list", "7 queued\n12 needs work\n")
    gh.on("go", "MAIN RED after batch\nquiet\n")
    gh.on("beads refill", "DEAD builder-a idle\n")
    gh.on("queue add", "queued 7\n")


def test_next_shows_the_batch_and_writes_nothing(rt, gh):
    batch(rt, gh)
    before = sorted(p.name for p in (rt.home / "train").iterdir())
    out, view = doc(rt, area.TrainNext, "release", "train", "next")
    assert out.exit_code == 0 and view.version == "0.9.36" and view.next_version == "0.9.37" and not view.started
    text = "\n".join(view.lines)
    assert "sm release train next: v0.9.36 is cut; v0.9.37 batch is OPEN" in text and "#2" in text and "#3" not in text
    assert "1 of 1 queued PRs are green with CI on current main" in text and "MAIN RED after batch" in text and "DEAD builder-a idle" in text
    assert "`sm release train next --start`" in text and "hz-" not in out.stdout
    assert sorted(p.name for p in (rt.home / "train").iterdir()) == before and not any("add" in call for call in gh.calls)
    assert [call[-2:] for call in gh.calls if "queue" in call] == [["list", "--active"]]


def test_next_start_queues_the_green_prs_and_records_the_batch_in_the_old_format(rt, gh):
    batch(rt, gh)
    out, view = doc(rt, area.TrainNext, "release", "train", "next", "--start")
    assert out.exit_code == 0 and view.started
    assert [call[-3:] for call in gh.calls if "add" in call] == [["queue", "add", "7"]]
    record = rt.home / "train" / "next-v0.9.37.json"
    data = json.loads(record.read_text())
    assert record.read_text() == json.dumps(data, indent=1, sort_keys=True)
    assert data["cut"] == "0.9.36" and data["green"] == [7] and data["started"] is True and data["opened_at"] == data["updated_at"]
    assert "recorded the train's next-v0.9.37.json" in view.lines and json.loads((rt.home / "train" / "v0.9.36.json").read_text())["phase"] == "release"


def test_next_lists_an_unknown_blocker_when_the_digest_gives_nothing(rt, gh):
    batch(rt, gh)
    gh.on("go", "")
    _out, view = doc(rt, area.TrainNext, "release", "train", "next")
    assert "blockers: 1" in view.lines and any(line.strip().startswith("UNKNOWN: the tick digest could not be read") for line in view.lines)


def test_next_without_a_cut_train_and_with_a_bad_version(rt, gh):
    (rt.home / "train" / "v0.9.35.json").unlink()  # done counts as cut; 0.9.36 is cleared
    out, view = doc(rt, area.TrainNext, "release", "train", "next")
    assert out.exit_code == 0 and view.version == "" and view.lines == ["sm release train next: no train has been cut; nothing to open"]
    assert invoke(rt, ["release", "train", "next", "latest"]).exit_code == 2


def test_plan_prints_the_candidate_table(rt, gh):
    (rt.home / "records" / "research").mkdir()
    (rt.home / "records" / "research" / "release-a.txt").write_text("# candidates\n701 #702\n")
    gh.on("pr view 701", {"number": 701, "state": "MERGED"})
    gh.on("pr view 702", {"number": 702, "state": "OPEN", "isDraft": False, "mergeStateStatus": "CLEAN", "statusCheckRollup": GREEN["statusCheckRollup"]})
    out, table = doc(rt, area.TrainPlan, "release", "train", "plan")
    assert out.exit_code == 0 and table.lines[0].split() == ["PR", "state", "merge", "CI", "min", "action"]
    assert table.lines[1].split() == ["701", "MERGED", "-", "-", "none"] and table.lines[2].split() == ["702", "OPEN", "CLEAN", "green", "sm", "head", "ship", "702"]
    assert table.lines[-1].startswith("merged 1/2, green-open 1") and gh.writes() == [] and "hz-" not in out.stdout
    text = invoke(rt, ["release", "train", "plan", "#702"]).stdout
    assert "702" in text and "701" not in text


def test_plan_without_prs_names_the_list_and_refuses_words(rt, gh):
    out, table = doc(rt, area.TrainPlan, "release", "train", "plan")
    assert out.exit_code == 2 and table.lines == [f"no PRs: pass numbers or list them in {rt.home / 'records' / 'research' / 'release-a.txt'}"]
    assert invoke(rt, ["release", "train", "plan", "abc"]).exit_code == 2 and gh.calls == []


def test_the_launchd_job_runs_one_tick_with_settings_that_exist():
    import plistlib

    from steuermaschine_heinzel.plugin import HeinzelSettings

    job = plistlib.loads((Path(__file__).parents[1] / "launchd" / "io.github.helmut-hoffer-von-ankershoffen.sm-train.plist").read_bytes())
    assert job["ProgramArguments"][1:] == ["daemon", "train"] and job["StartInterval"] == 600 and "KeepAlive" not in job
    names = [key.removeprefix("SM_HEINZEL__").lower() for key in job["EnvironmentVariables"] if key.startswith("SM_HEINZEL__")]
    assert names == ["release_train_every_minutes"] and all(name in HeinzelSettings.model_fields for name in names)
    assert not any(key.startswith("HZ_") for key in job["EnvironmentVariables"])


def test_the_checked_in_launchd_plists_are_templates_that_install_with_the_real_home():
    import plistlib

    for plist in (Path(__file__).parents[1] / "launchd").glob("*.plist"):
        text = plist.read_text()
        assert "/Users/USER" in text
        installed = text.replace("/Users/USER", "/Users/someone")
        assert "/Users/USER" not in installed
        assert plistlib.loads(installed.encode())["ProgramArguments"][0] == "/Users/someone/.local/bin/sm"
