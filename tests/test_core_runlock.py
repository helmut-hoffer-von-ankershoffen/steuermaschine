"""The run lock in one process, with a fake clock and scripted commands. The cross-process behaviour (two real
commands excluding each other) is tested through the command that uses it."""

import fcntl
import json

from steuermaschine.protocols import Result
from steuermaschine.runlock import Policy, Refusal, Request, RunLock, stricter
from steuermaschine.testing import FakeClock


class Commands:
    def __init__(self, code=0, parents=None):
        self.code, self.parents, self.ran = code, parents or {}, []

    def run(self, argv, cwd=None, timeout=None, env=None):
        return Result(0, f"{self.parents.get(int(argv[-1]), 0)}\n")

    def passthrough(self, argv, cwd=None, env=None, timeout=None):
        self.ran.append((list(argv), dict(env or {}), timeout))
        return self.code


def lock(tmp_path, load=5.0, commands=None, **policy):
    said = []
    policy.setdefault("worker_caps", {"WORKERS": 8})
    rl = RunLock(Policy(directory=tmp_path, history=tmp_path / "history.jsonl", **policy), commands or Commands(), FakeClock(), load=lambda: load, say=said.append,
                 pid=lambda: 4242, parent_pid=lambda: 4000)
    return rl, said


def test_a_heavy_run_takes_slot_zero_records_itself_caps_workers_and_logs(tmp_path):
    rl, said = lock(tmp_path, commands=Commands(code=7))
    out = rl.run(Request(argv=["pytest", "tests/x.py"], environ={"WORKERS": "32", "KEEP": "1"}, cwd="/w"))
    assert (out.status, out.exit_code, out.lane) == ("ran", 7, "heavy")
    _argv, env, timeout = rl.commands.ran[0]
    assert env["WORKERS"] == "8" and env["KEEP"] == "1" and env["SM_GATE_HELD"] == "4242" and timeout is None
    assert (tmp_path / "runlock.lock").read_text() == "4242 /w pytest tests/x.py\n"
    assert json.loads((tmp_path / "history.jsonl").read_text())["exit"] == 7 and "acquired after 0s" in said[0]


def test_it_gives_up_at_the_load_bar_under_a_hold_and_when_every_slot_is_taken(tmp_path):
    rl, _ = lock(tmp_path, load=40.0)
    out = rl.run(Request(argv=["true"], max_wait_seconds=11, priority=2))
    assert (out.status, out.exit_code) == ("gave_up", 75) and "load 40 is at or above 40" in out.reason and rl.commands.ran == []
    rl, _ = lock(tmp_path)
    rl.p.hold.touch()
    rl.clock.t = rl.p.hold.stat().st_mtime + 5
    assert "a hold is set" in rl.run(Request(argv=["true"], max_wait_seconds=6)).reason
    assert rl.run(Request(argv=["true"], hold_exempt=True)).status == "ran"
    rl.p.hold.unlink()
    with rl.p.slot_file(0).open("a+") as taken:
        fcntl.flock(taken, fcntl.LOCK_EX)
        taken.truncate(0)
        taken.write("77 /w sleep 9\n")
        taken.flush()
        one, said = lock(tmp_path, heavy_slots=1)
        out = one.run(Request(argv=["true"], max_wait_seconds=61))
        assert out.exit_code == 75 and "held by 77 /w sleep 9" in out.reason and len([s for s in said if "waiting" in s]) == 2
        status = one.status()
        assert status.slots_in_use == 1 and "heavy slots in use 1/1, workers per gate 9" in status.render()[0]
        two, _ = lock(tmp_path, load=19.0)
        assert two.run(Request(argv=["true"])).lane == "heavy" and two.commands.ran[0][1]["WORKERS"] == "4"  # second slot: the budget is shared
        busy, _ = lock(tmp_path, load=20.0)
        assert busy.run(Request(argv=["true"], max_wait_seconds=6)).exit_code == 75  # no second slot at load 20


def test_refusals_come_first_and_light_commands_skip_the_heavy_slot(tmp_path):
    no = lambda argv, environ: Refusal(2, "not like this") if argv[0] == "make" else None  # noqa: E731
    rl, said = lock(tmp_path, refusals=(no,), is_light=lambda argv: argv[0] == "docs", child_env=lambda env: env.update(EXTRA="1"))
    out = rl.run(Request(argv=["make", "pr"]))
    assert (out.status, out.exit_code, out.reason) == ("refused", 2, "not like this") and rl.commands.ran == []
    assert rl.run(Request(argv=[])).exit_code == 2
    out = rl.run(Request(argv=["docs"], timeout_seconds=5))
    assert out.lane == "light" and rl.commands.ran[0][1]["EXTRA"] == "1" and rl.commands.ran[0][2] == 5 and "light lane" in said[0]
    assert (tmp_path / "runlock.light.0.lock").read_text() == "4242\n"
    handles = [rl.p.light_slot_file(n).open("a+") for n in range(4)]
    for handle in handles:
        fcntl.flock(handle, fcntl.LOCK_EX)
    out = rl.run(Request(argv=["docs"], max_wait_seconds=3))
    assert (out.status, out.exit_code, out.lane) == ("gave_up", 75, "light")
    for handle in handles:
        handle.close()


def test_a_command_under_a_real_holder_runs_at_once_and_a_forged_marker_does_not(tmp_path):
    commands = Commands(parents={4000: 3000, 3000: 1})
    rl, _ = lock(tmp_path, commands=commands, heavy_slots=1)
    with rl.p.slot_file(0).open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        held.truncate(0)
        held.write("3000 /w outer\n")
        held.flush()
        assert rl.run(Request(argv=["inner"], environ={"SM_GATE_HELD": "3000"})).lane == "nested"
        assert rl.run(Request(argv=["inner"], environ={"SM_GATE_HELD": "999"}, max_wait_seconds=1)).exit_code == 75  # not an ancestor
        assert rl.run(Request(argv=["inner"], environ={"SM_GATE_HELD": "x; rm"}, max_wait_seconds=1)).exit_code == 75
    assert rl.holder_lane({"SM_GATE_HELD": "3000"}) is None  # an ancestor whose slot is no longer locked holds nothing


def test_priority_waits_behind_urgent_and_normal_behind_priority(tmp_path):
    import os

    rl, _ = lock(tmp_path)
    rl._prepare()
    (rl.p.urgent / str(os.getpid())).touch()  # a live urgent waiter
    assert rl.run(Request(argv=["true"], priority=1, max_wait_seconds=1)).exit_code == 75
    assert rl.run(Request(argv=["true"], priority=0, max_wait_seconds=1)).reason.count("priority gates are ahead") == 1
    assert rl.run(Request(argv=["true"], priority=2)).status == "ran"
    (rl.p.urgent / str(os.getpid())).unlink()
    (rl.p.waiting / "999999999").touch()  # a dead waiter is swept
    assert rl.run(Request(argv=["true"])).status == "ran" and not list(rl.p.waiting.iterdir())
    assert stricter("auto", 8) == 8 and stricter("0", 8) == 1 and stricter(3, 8) == 3


def test_an_exclusive_run_waits_for_an_idle_machine_and_then_keeps_it(tmp_path):
    rl, said = lock(tmp_path, load=25.0)
    with rl.p.slot_file(1).open("a+") as other:  # a second heavy run is still going
        fcntl.flock(other, fcntl.LOCK_EX)
        out = rl.run(Request(argv=["git", "push"], exclusive=True, max_wait_seconds=11))
        assert out.exit_code == 75 and "waits for the machine to be idle (1 heavy run(s) still going)" in out.reason and rl.commands.ran == []
    seen = []

    class Probe(Commands):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):  # what another command sees while the push runs
            other, _ = lock(tmp_path, load=5.0)
            seen.append((other.status().exclusive, other.run(Request(argv=["pytest", "t.py"], max_wait_seconds=1, priority=2))))
            return 0

    rl, said = lock(tmp_path, load=25.0, commands=Probe())
    out = rl.run(Request(argv=["git", "push"], exclusive=True))
    assert (out.status, out.lane) == ("ran", "heavy") and "exclusive: no other heavy run starts" in said[0]
    held, refused = seen[0]
    assert held and refused.exit_code == 75 and "an exclusive run (a critical-path push) holds the machine" in refused.reason
    assert not rl.exclusive_held() and rl.run(Request(argv=["true"])).status == "ran"  # released at the end


def test_an_exclusive_run_still_respects_the_load_bar(tmp_path):
    rl, _ = lock(tmp_path, load=40.0)
    assert "load 40" in rl.run(Request(argv=["git", "push"], exclusive=True, max_wait_seconds=1)).reason


# ---- light lane by command and by learned p50, no rerun of a failure on the same tree, richer history rows


def history(tmp_path):
    return [json.loads(line) for line in (tmp_path / "history.jsonl").read_text().splitlines()]


def test_a_command_that_matches_the_light_patterns_runs_in_the_light_lane_and_is_logged(tmp_path):
    rl, _ = lock(tmp_path)
    with rl.p.slot_file(0).open("a+") as taken:  # the heavy slot is taken: a light command must not wait for it
        fcntl.flock(taken, fcntl.LOCK_EX)
        out = rl.run(Request(argv=["ruff", "check", "src"], max_wait_seconds=1, environ={"SM_CALLER": "builder-7"}))
    assert (out.status, out.lane) == ("ran", "light")
    row = history(tmp_path)[0]
    assert (row["lane"], row["caller"], row["failure"], row["exit"]) == ("light", "builder-7", "", 0)


def row(argv, ran_s, exit_code, cwd="", environ=None):
    return {"cmd": " ".join(argv)[:200], "key": RunLock.history_key(argv, cwd, environ), "ran_s": ran_s, "exit": exit_code}


def test_a_command_with_a_short_green_p50_is_learned_light_but_a_failed_or_slow_history_is_not(tmp_path):
    rl, _ = lock(tmp_path)
    argv = ["pytest", "tests/unit/a_test.py"]
    path = tmp_path / "history.jsonl"
    path.write_text("".join(json.dumps(row(argv, 10, 0)) + "\n" for _ in range(2)) + "not json\n[1]\n" + json.dumps({"cmd": " ".join(argv), "ran_s": 1, "exit": 0}) + "\n")
    assert not rl.learned_light(argv)  # two green runs are not enough; a row without a key (an older version) teaches nothing
    with path.open("a") as handle:
        handle.write(json.dumps(row(argv, 20, 0)) + "\n")
        handle.write(json.dumps(row(argv, 1, 1)) + "\n")  # a fast failure does not pull the p50 down
    assert rl.learned_light(argv) and rl.run(Request(argv=argv)).lane == "light"
    with path.open("a") as handle:
        handle.write("".join(json.dumps(row(argv, 300, 0)) + "\n" for _ in range(30)))
    assert not rl.learned_light(argv) and rl.run(Request(argv=argv)).lane == "heavy"
    assert rl.run(Request(argv=argv, exclusive=True)).lane == "heavy"


def test_probe_light_patterns_match_the_whole_argv_so_extra_targets_are_heavy(tmp_path):
    rl, _ = lock(tmp_path)
    assert rl.is_light_command(["make", "lint"]) and rl.is_light_command(["uv", "run", "ruff", "check", "src", "tests"])
    assert rl.is_light_command(["git", "status", "--short"])
    for argv in (["make", "lint", "test"], ["make", "lint", "check"], ["make", "lint test"],
                 ["git", "-c", "x=y", "status"], ["git", "push", "origin"], ["make", "lintx"], ["ruff"], ["ruff", "version"]):
        assert not rl.is_light_command(argv), argv


def test_probe_the_p50_is_keyed_by_the_full_argv_the_directory_and_the_scope_variables(tmp_path):
    rl, _ = lock(tmp_path)
    files = [f"tests/unit/t{i}_" + "x" * 30 + ".py" for i in range(8)]  # long enough that a 200-character cut hides what follows
    argv = ["pytest", *files]
    path = tmp_path / "history.jsonl"
    path.write_text("".join(json.dumps(row(argv, 5, 0, "/w1")) + "\n" for _ in range(3)))
    assert rl.learned_light(argv, "/w1")
    assert not rl.learned_light([*argv, "tests/integration/", "tests/e2e/"], "/w1")  # the same list plus more targets
    assert len(" ".join(argv)) > 200 and not rl.learned_light([*argv[:-1], "tests/unit/other.py"], "/w1")
    assert not rl.learned_light(argv, "/w2")  # another repository or worktree
    assert not rl.learned_light(argv, "/w1", {"PYTEST_ADDOPTS": "-m slow"})  # scope by environment
    assert rl.learned_light(argv, "/w1", {"UNRELATED": "1"})
    assert rl.run(Request(argv=[*argv, "tests/e2e/"], cwd="/w1")).lane == "heavy"


def test_a_force_rerun_reason_must_be_a_reason(tmp_path):
    rl, _ = lock(tmp_path, tree_state=lambda cwd: "T")
    for bad in ("--", "-x something long enough", "short", "   "):
        out = rl.run(Request(argv=["pytest", "x"], force_rerun=bad))
        assert (out.status, out.exit_code) == ("refused", 2) and "--force-rerun needs a reason" in out.reason, bad
    assert rl.commands.ran == []


def test_a_failed_command_is_refused_on_the_same_tree_until_the_tree_changes_or_a_reason_is_given(tmp_path):
    tree = ["T1"]
    rl, _ = lock(tmp_path, commands=Commands(code=1), tree_state=lambda cwd: tree[0])
    first = rl.run(Request(argv=["pytest", "tests/x.py"], cwd="/w", environ={"SM_CALLER": "b1"}))
    assert (first.status, first.exit_code) == ("ran", 1)
    row = history(tmp_path)[0]
    assert (row["tree"], row["caller"], row["failure"], row["lane"]) == ("T1", "b1", "test", "heavy")
    again = rl.run(Request(argv=["pytest", "tests/x.py"], cwd="/w"))
    assert (again.status, again.exit_code) == ("refused", 2) and "T1" in again.reason and "--force-rerun" in again.reason
    assert len(rl.commands.ran) == 1
    tree[0] = "T1+dirty"
    assert rl.run(Request(argv=["pytest", "tests/x.py"], cwd="/w")).status == "ran"
    tree[0] = "T1"
    forced = rl.run(Request(argv=["pytest", "tests/x.py"], cwd="/w", force_rerun="flaky network fixture, see bead"))
    assert forced.status == "ran" and history(tmp_path)[-1]["forced"] == "flaky network fixture, see bead"


def test_a_green_rerun_clears_the_refusal_and_timeouts_usage_errors_and_pushes_are_exempt(tmp_path):
    codes = Commands(code=1)
    rl, _ = lock(tmp_path, commands=codes, tree_state=lambda cwd: "T")
    assert rl.run(Request(argv=["mypy", "-p", "x"])).exit_code == 1 and history(tmp_path)[0]["failure"] == "type"
    assert rl.run(Request(argv=["mypy", "-p", "x"])).status == "refused"
    codes.code = 124
    assert rl.run(Request(argv=["make", "build"], force_rerun="a real reason here")).exit_code == 124 and history(tmp_path)[-1]["failure"] == "infra-timeout"
    assert rl.run(Request(argv=["make", "build"])).status == "ran"  # a timeout says nothing about the tree
    codes.code = 127
    rl.run(Request(argv=["nosuch"]))
    assert history(tmp_path)[-1]["failure"] == "usage" and rl.run(Request(argv=["nosuch"])).status == "ran"
    codes.code = 1
    rl.run(Request(argv=["git", "push", "origin", "HEAD"]))
    assert rl.run(Request(argv=["git", "push", "origin", "HEAD"])).status == "ran"
    codes.code = 0
    assert rl.run(Request(argv=["mypy", "-p", "x"], force_rerun="the cause was fixed outside the tree")).exit_code == 0
    assert rl.run(Request(argv=["mypy", "-p", "x"])).status == "ran"  # the last row on this tree is green now


def test_failure_classes():
    from steuermaschine.runlock import failure_class

    assert failure_class(["ruff", "check"], 1) == "lint" and failure_class(["pyright"], 1) == "type"
    assert failure_class(["pytest", "--bogus"], 4) == "usage" and failure_class(["python", "x.py"], 2) == "usage"
    assert failure_class(["bin/check"], 1) == "test" and failure_class(["make", "x"], 3) == "other" and failure_class(["pytest"], 0) == ""


def test_git_tree_state_hashes_the_tree_and_the_dirty_state():
    from steuermaschine.runlock import git_tree_state

    class Git:
        def __init__(self, **out):
            self.out = out

        def run(self, argv, cwd=None, timeout=None):
            key = argv[1]
            return Result(self.out.get(key + "_rc", 0), self.out.get(key, ""))

    clean = git_tree_state(Git(**{"rev-parse": "abc\n"}))("/w")
    assert clean == "abc"
    dirty = git_tree_state(Git(**{"rev-parse": "abc\n", "diff": "+x\n"}))("/w")
    other = git_tree_state(Git(**{"rev-parse": "abc\n", "diff": "+y\n"}))("/w")
    untracked = git_tree_state(Git(**{"rev-parse": "abc\n", "ls-files": "new.py\n", "hash-object": "h1\n"}))("/w")
    assert dirty.startswith("abc+") and len({clean, dirty, other, untracked}) == 4
    assert git_tree_state(Git(**{"rev-parse_rc": 128}))("/w") is None and git_tree_state(Git(**{"rev-parse": "abc", "diff_rc": 1}))("/w") is None


def test_status_shows_both_lanes(tmp_path):
    rl, _ = lock(tmp_path)
    with rl.p.light_slot_file(1).open("a+") as taken:
        fcntl.flock(taken, fcntl.LOCK_EX)
        taken.write("77\n")
        taken.flush()
        status = rl.status()
    assert (status.light_slots_in_use, status.light_slots, status.light_holders) == (1, 4, ["pid 77"])
    text = "\n".join(status.render())
    assert "heavy slots in use 0/3" in text and "light slots in use 1/4" in text and "light: pid 77" in text
