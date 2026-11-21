"""The run lock in one process, with a fake clock and scripted commands. The cross-process behaviour (two real
commands excluding each other) is tested through the command that uses it."""

import fcntl
import json
import os

from steuermaschine.protocols import Result
from steuermaschine.runlock import PUSH_COMMANDS, Policy, Refusal, Request, RunLock, stricter
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


# ---- scheduling: the start bar, the reserved slot of the focus head, the push lane, and who holds or waits and why


class Ps(Commands):
    """Answers the CPU listing of `status` as well as the parent lookup."""

    def run(self, argv, cwd=None, timeout=None, env=None):
        if argv[:2] == ["ps", "-Ao"]:
            return Result(0, " 12.5  100 /usr/bin/idle\n320.0  200 /opt/bin/node\n  0.0  300 sleepy\n 88,0  400 pytest worker\nnonsense\n")
        return super().run(argv, cwd=cwd, timeout=timeout, env=env)


def test_no_heavy_command_starts_at_or_above_the_start_bar_and_the_wait_names_the_load(tmp_path):
    rl, said = lock(tmp_path, load=10.0, start_load=10.0)
    out = rl.run(Request(argv=["pytest", "t.py"], max_wait_seconds=6, priority=2))
    assert (out.status, out.exit_code) == ("gave_up", 75) and rl.commands.ran == []
    assert "load 10 is at or above 10: no heavy command starts above it" in out.reason and "waiting 0s: load 10 is at or above 10" in said[0]
    below, _ = lock(tmp_path, load=9.9, start_load=10.0)
    assert below.run(Request(argv=["pytest", "t.py"])).status == "ran"
    off, _ = lock(tmp_path, load=39.0)  # no start bar: only max_load counts, as before
    assert off.run(Request(argv=["pytest", "t.py"])).status == "ran"
    light, _ = lock(tmp_path, load=30.0, start_load=10.0)  # the light lane has no load bar
    assert light.run(Request(argv=["ruff", "check"], max_wait_seconds=1)).lane == "light"


def test_a_command_that_waits_for_the_load_starts_by_itself_when_it_falls(tmp_path):
    rl, said = lock(tmp_path, start_load=10.0)
    rl.load = lambda: 6.0 if len(rl.clock.slept) >= 2 else 12.0  # the load of a gate that just ended, falling
    out = rl.run(Request(argv=["pytest", "b.py"]))
    assert out.status == "ran" and out.waited_seconds == 10 and "waiting 0s: load 12 is at or above 10" in said[0] and "acquired after 10s" in said[1]


def test_the_last_slot_is_reserved_for_the_focus_head_and_goes_by_its_own_bar(tmp_path):
    focus = lambda cwd: "/work/release-fix" if cwd.startswith("/work/release-fix") else ""  # noqa: E731
    policy = dict(heavy_slots=2, reserved_slots=1, start_load=10.0, extra_slot_load=20.0, focus=focus)
    with lock(tmp_path)[0].p.slot_file(0).open("a+") as taken:  # the one general slot is in use
        fcntl.flock(taken, fcntl.LOCK_EX)
        taken.truncate(0)
        taken.write("77 /w sleep 9\n")
        taken.flush()
        other, _ = lock(tmp_path, load=5.0, **policy)
        out = other.run(Request(argv=["pytest", "t.py"], cwd="/work/other", max_wait_seconds=6))
        assert out.exit_code == 75 and "held by 77 /w sleep 9; 1 more slot(s) are reserved for a focus head" in out.reason
        head, _ = lock(tmp_path, load=15.0, **policy)  # above the start bar, below the focus head's
        out = head.run(Request(argv=["pytest", "t.py"], cwd="/work/release-fix/tests"))
        assert (out.status, out.lane) == ("ran", "heavy") and head.commands.ran[0][1]["WORKERS"] == "4"  # the budget is shared
        hot, _ = lock(tmp_path, load=20.0, **policy)
        out = hot.run(Request(argv=["pytest", "t.py"], cwd="/work/release-fix", max_wait_seconds=6))
        assert out.exit_code == 75 and "load 20 is at or above 20" in out.reason and "the bar of a focus head" in out.reason
        status = other.status()
        assert status.reserved_slots == 1 and "1 of the 2 heavy slots reserved for a focus head" in "\n".join(status.render())
    alone, _ = lock(tmp_path, heavy_slots=1, reserved_slots=1)  # the only slot is never reserved
    assert alone.p.general_slots == 1 and alone.run(Request(argv=["pytest", "t.py"], cwd="/work/other")).status == "ran"
    broken, _ = lock(tmp_path, heavy_slots=2, reserved_slots=1, focus=lambda cwd: (_ for _ in ()).throw(OSError("no store")))
    assert broken.run(Request(argv=["pytest", "t.py"], cwd="/work/other")).status == "ran"  # an unreadable focus list stops nobody


def test_a_push_is_exclusive_by_itself_gets_the_machine_alone_and_waits_for_a_quiet_one(tmp_path):

    seen = []

    class Probe(Commands):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):  # what another command sees while the push runs
            other, _ = lock(tmp_path, load=5.0)
            seen.append((other.status().exclusive, other.run(Request(argv=["pytest", "t.py"], max_wait_seconds=1, priority=2)).exit_code))
            return 0

    rl, said = lock(tmp_path, commands=Probe(), exclusive_commands=PUSH_COMMANDS, start_load=10.0)
    out = rl.run(Request(argv=["git", "push", "origin", "HEAD:refs/heads/fix/x"], cwd="/w"))
    assert (out.status, out.lane) == ("ran", "heavy") and "exclusive: no other heavy run starts" in said[0] and seen == [(True, 75)]
    wrapped, said = lock(tmp_path, exclusive_commands=PUSH_COMMANDS)
    assert wrapped.run(Request(argv=["env", "NO_PREFLIGHT=1", "git", "push"])).status == "ran" and "exclusive" in said[0]
    for argv in (["git", "pushx"], ["git", "status"], ["pytest", "git push"], ["sh", "-c", "git push"]):
        plain, said = lock(tmp_path, exclusive_commands=PUSH_COMMANDS)
        assert plain.run(Request(argv=argv)).status == "ran" and "exclusive" not in said[0], argv
    docs, said = lock(tmp_path, exclusive_commands=PUSH_COMMANDS, is_light=lambda argv: True)  # a push the policy calls light stays light
    assert docs.run(Request(argv=["git", "push"])).lane == "light"
    with rl.p.slot_file(1).open("a+") as busy:  # a heavy run is going: the push waits for an idle machine
        fcntl.flock(busy, fcntl.LOCK_EX)
        waits, _ = lock(tmp_path, exclusive_commands=PUSH_COMMANDS)
        out = waits.run(Request(argv=["git", "push"], max_wait_seconds=6))
        assert out.exit_code == 75 and "waits for the machine to be idle (1 heavy run(s) still going)" in out.reason
    loud, _ = lock(tmp_path, load=11.0, exclusive_commands=PUSH_COMMANDS, start_load=10.0, extra_slot_load=20.0, focus=lambda cwd: "/w")
    out = loud.run(Request(argv=["git", "push"], cwd="/w", max_wait_seconds=6))  # also a focus head's push wants the quiet machine
    assert out.exit_code == 75 and "load 11 is at or above 10" in out.reason and "focus head" not in out.reason


def test_a_push_that_waits_holds_new_heavy_runs_off(tmp_path):
    import os

    rl, _ = lock(tmp_path)
    rl._prepare()
    (rl.p.urgent / str(os.getpid())).touch()  # a push waits for the machine (an exclusive request queues as urgent)
    out = rl.run(Request(argv=["pytest", "t.py"], max_wait_seconds=1))
    assert out.exit_code == 75 and "priority gates are ahead" in out.reason


def test_status_names_who_holds_who_waits_and_why(tmp_path):
    import os

    seen = {}

    class Inside(Ps):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):  # while the heavy run goes on
            waiter = RunLock(Policy(directory=tmp_path, start_load=10.0, heavy_slots=1), Ps(), FakeClock(), load=lambda: 4.0, pid=lambda: os.getppid())
            gave_up = waiter.run(Request(argv=["pytest", "tests/b.py"], cwd="/work/b", max_wait_seconds=1, environ={"SM_CALLER": "builder-b"}))
            assert gave_up.exit_code == 75 and not (waiter.p.runs / f"{os.getppid()}.json").exists()  # a request that gave up leaves no record
            record = Policy(directory=tmp_path).runs / f"{os.getppid()}.json"
            waiter._note(Request(argv=["pytest", "tests/b.py"], cwd="/work/b", environ={"SM_CALLER": "builder-b"}), "waiting", FakeClock().now() - 90,
                         "the heavy slot is held by someone")
            (waiter.p.runs / "999999999.json").write_text("{}")  # a record whose process is gone
            assert record.exists()
            seen["status"] = RunLock(Policy(directory=tmp_path, start_load=10.0, heavy_slots=1), Ps(), FakeClock(), load=lambda: 12.0).status()
            record.unlink()
            return 0

    rl = RunLock(Policy(directory=tmp_path, start_load=10.0, heavy_slots=1, focus=lambda cwd: "/work/a"), Inside(), FakeClock(), load=lambda: 4.0, pid=os.getpid)
    assert rl.run(Request(argv=["pytest", "tests/a.py"], cwd="/work/a")).status == "ran"
    status = seen["status"]
    running, waiting = status.runs
    assert (running.pid, running.state, running.kind, running.cwd, running.command, running.focus) == (os.getpid(), "running", "focus", "/work/a", "pytest tests/a.py", "/work/a")
    assert (waiting.pid, waiting.state, waiting.kind, waiting.caller, waiting.seconds) == (os.getppid(), "waiting", "heavy", "builder-b", 90.0)
    assert not (tmp_path / "runlock.runs" / "999999999.json").exists()
    text = "\n".join(status.render())
    assert f"held by {os.getpid()} /work/a pytest tests/a.py [focus, 0s]" in text
    assert f"waiting: pid {os.getppid()} [heavy] 90s /work/b: pytest tests/b.py -- the heavy slot is held by someone" in text
    assert "start bar: no heavy start at or above load 10 (NO heavy command starts now)" in text
    assert status.busiest == ["pid 200 node 320 %", "pid 400 pytest worker 88 %", "pid 100 idle 12 %"] and "the load is made by: pid 200 node 320 %" in text
    after = rl.status()
    assert after.runs == [] and after.busiest == [] and "(open)" in "\n".join(after.render())  # the record goes with the run; below the bar nobody is named


# ---- the fast lane: checks and pushes run beside each other at any load; only --full is heavy, exclusive and under the load bars

def fast(tmp_path, load=5.0, **policy):
    policy.setdefault("fast_slots", 3)
    return lock(tmp_path, load=load, **policy)


def test_a_fast_check_runs_at_any_load_in_the_fast_lane_with_a_share_of_the_workers(tmp_path):
    rl, said = fast(tmp_path, load=75.0, start_load=10.0, max_load=40.0)
    out = rl.run(Request(argv=["pytest", "tests/x.py"], environ={"WORKERS": "32"}, cwd="/w", timeout_seconds=120))
    assert (out.status, out.lane, out.exit_code) == ("ran", "fast", 0) and "acquired after 0s" in said[0]
    _argv, env, timeout = rl.commands.ran[0]
    assert env["WORKERS"] == "8" and timeout == 120 and json.loads((tmp_path / "history.jsonl").read_text())["lane"] == "fast"
    full = rl.run(Request(argv=["pytest", "tests/x.py"], full=True, max_wait_seconds=6))  # the same load stops a full run
    assert (full.status, full.exit_code, full.lane) == ("gave_up", 75, "heavy") and "load 75 is at or above 40" in full.reason and "--full" in full.reason


def test_a_push_is_exclusive_only_when_it_is_a_full_run(tmp_path):
    rl, said = fast(tmp_path, exclusive_commands=PUSH_COMMANDS, start_load=10.0)
    assert rl.run(Request(argv=["git", "push", "origin", "HEAD"])).lane == "fast" and "exclusive" not in said[0]
    with rl.p.fast_slot_file(0).open("a+") as taken:  # a fast run beside it does not stop another fast push
        fcntl.flock(taken, fcntl.LOCK_EX)
        assert rl.run(Request(argv=["git", "push"])).lane == "fast"
        out = rl.run(Request(argv=["git", "push"], full=True, max_wait_seconds=6))  # a full push waits for the machine to be idle
        assert out.exit_code == 75 and "waits for the machine to be idle (1 heavy run(s) still going)" in out.reason
    out = rl.run(Request(argv=["git", "push"], full=True))
    assert (out.lane, "exclusive: no other heavy run starts" in said[-1]) == ("heavy", True)
    old, said = lock(tmp_path, exclusive_commands=PUSH_COMMANDS)
    # a policy without a fast lane keeps the push lane of every push
    assert old.run(Request(argv=["git", "push"])).lane == "heavy" and "exclusive: no other heavy run starts" in said[0]


def test_the_fast_cap_is_three_and_drops_with_the_load_and_the_waiter_is_told_why(tmp_path):
    held = []
    try:
        for n in range(3):
            handle = tmp_path.joinpath(f"runlock.fast.{n}.lock").open("a+")
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(f"{900 + n} /w pytest t{n}.py\n")
            handle.flush()
            held.append(handle)
        rl, said = fast(tmp_path)
        assert rl.fast_cap() == 3
        out = rl.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=61))
        assert (out.exit_code, out.lane) == (75, "fast") and "all 3 fast slot(s) are taken" in out.reason and "Nothing ran" in out.reason
        assert any("Fast checks run up to 3 at once at any load" in line for line in said)
        status = rl.status()
        assert (status.fast_slots, status.fast_slots_in_use, status.fast_cap) == (3, 3, 3)
        assert "fast slots in use 3/3" in "\n".join(status.render())
        held.pop().close()  # one frees up: the next fast run starts
        assert rl.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=6)).status == "ran"
    finally:
        for handle in held:
            handle.close()
    for load, cap in ((19.9, 3), (20.0, 2), (39.9, 2), (60.0, 1)):
        assert fast(tmp_path, load=load)[0].fast_cap() == cap
    busy, _ = fast(tmp_path, load=60.0)
    with busy.p.fast_slot_file(0).open("a+") as one:  # at load 60 the cap is one: a second waits
        fcntl.flock(one, fcntl.LOCK_EX)
        assert "the cap is 1 of 3 at load 60" in busy.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3)).reason


def test_a_fast_run_waits_for_a_full_run_that_holds_the_machine_and_for_a_hold(tmp_path):
    rl, _ = fast(tmp_path)
    seen = []

    class Probe(Commands):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):
            other, _ = fast(tmp_path)
            seen.append(other.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3)))
            return 0

    full, _ = fast(tmp_path, commands=Probe())
    assert full.run(Request(argv=["pytest", "tests/b.py"], full=True, exclusive=True)).status == "ran"
    assert seen[0].exit_code == 75 and "a full run (--full, exclusive) holds the machine" in seen[0].reason
    rl.p.hold.touch()
    rl.clock.t = rl.p.hold.stat().st_mtime + 5
    assert "a hold is set" in rl.run(Request(argv=["true"], max_wait_seconds=3)).reason
    assert rl.run(Request(argv=["true"], hold_exempt=True)).lane == "fast"


def test_a_command_inside_a_fast_run_runs_at_once(tmp_path):
    class Inner(Commands):
        def passthrough(self, argv, cwd=None, env=None, timeout=None):
            inner, _ = fast(tmp_path, commands=Commands(), load=75.0)
            inner.parent_pid = lambda: 4242
            seen.append(inner.run(Request(argv=["pytest", "tests/b.py"], environ=env, max_wait_seconds=1)))
            return 0

    seen = []
    rl, _ = fast(tmp_path, commands=Inner())
    assert rl.run(Request(argv=["git", "push"])).lane == "fast"
    assert [(o.status, o.lane) for o in seen] == [("ran", "nested")]


def alive_lock(tmp_path, clock=None, load=5.0, **policy):
    """A lock whose pid is this process's: the markers it writes name a live process."""
    said = []
    policy.setdefault("fast_slots", 3)
    rl = RunLock(Policy(directory=tmp_path, history=tmp_path / "history.jsonl", **policy), Commands(), clock or FakeClock(), load=lambda: load, say=said.append,
                 pid=os.getpid, parent_pid=lambda: 4000)
    return rl, said


def test_a_waiting_exclusive_run_holds_new_fast_runs_back_so_fast_runs_cannot_starve_it(tmp_path):
    seen = []

    class Clock(FakeClock):
        def sleep(self, seconds):  # while the exclusive run waits for the running fast work to end
            super().sleep(seconds)
            other, _ = alive_lock(tmp_path, FakeClock())
            seen.append((len(list(other.p.draining.iterdir())), other.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3)),
                         other.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3, priority=2))))

    with tmp_path.joinpath("runlock.fast.0.lock").open("a+") as running:  # one fast run is going
        fcntl.flock(running, fcntl.LOCK_EX)
        rl, _ = alive_lock(tmp_path, Clock())
        out = rl.run(Request(argv=["git", "push"], full=True, exclusive=True, max_wait_seconds=6))
    assert out.exit_code == 75 and "exclusive run waits for the machine to be idle (1 heavy run(s) still going)" in out.reason
    waiting, normal, urgent = seen[0]
    assert waiting == 1 and normal.exit_code == 75 and "no new fast check starts before it" in normal.reason  # the writer goes first
    assert urgent.lane == "fast" and urgent.status == "ran"  # a priority run (the train's) is not held back
    assert list(rl.p.draining.iterdir()) == [] and rl.run(Request(argv=["true"])).lane == "fast"  # gone with the request: fast runs start again


def test_an_exclusive_run_that_waits_for_the_load_does_not_hold_fast_runs_back(tmp_path):
    seen = []

    class Clock(FakeClock):
        def sleep(self, seconds):
            super().sleep(seconds)
            other, _ = alive_lock(tmp_path, FakeClock(), load=75.0)
            seen.append(other.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3)))

    rl, _ = alive_lock(tmp_path, Clock(), load=75.0, start_load=10.0)
    assert "load 75" in rl.run(Request(argv=["git", "push"], full=True, exclusive=True, max_wait_seconds=6)).reason
    assert seen and all(o.status == "ran" and o.lane == "fast" for o in seen)  # waiting for the load starves nobody


def test_a_fast_run_with_a_priority_goes_ahead_of_those_without(tmp_path):
    rl, _ = alive_lock(tmp_path)
    rl.p.fast_waiting.mkdir(parents=True, exist_ok=True)
    (rl.p.fast_waiting / f"2-{os.getppid()}").touch()  # a priority-2 request waits (the train's: --priority 2 --hold-exempt)
    plain = rl.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3))
    assert plain.exit_code == 75 and "a fast check with a higher priority goes first" in plain.reason
    one = rl.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3, priority=1))
    assert one.exit_code == 75  # priority 1 also waits for priority 2
    assert rl.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3, priority=2, hold_exempt=True)).status == "ran"
    (rl.p.fast_waiting / f"2-{os.getppid()}").unlink()
    assert rl.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3)).status == "ran"
    assert list(rl.p.fast_waiting.iterdir()) == []  # a request leaves no marker behind


def test_an_exclusive_run_that_waits_for_the_load_leaves_fast_runs_one_slot(tmp_path):
    seen = []

    class Clock(FakeClock):
        def sleep(self, seconds):
            super().sleep(seconds)
            other, _ = alive_lock(tmp_path, FakeClock(), load=12.0)
            seen.append((len(list(other.p.load_waiting.iterdir())), other.fast_cap(), other.run(Request(argv=["pytest", "tests/a.py"], max_wait_seconds=3))))

    with tmp_path.joinpath("runlock.fast.0.lock").open("a+") as running:  # slot 0 is taken: with the cap of 3 slot 1 would be free
        fcntl.flock(running, fcntl.LOCK_EX)
        quiet, _ = alive_lock(tmp_path, load=12.0)
        assert quiet.fast_cap() == 3 and quiet.run(Request(argv=["true"])).status == "ran"  # no exclusive run waits: three slots
        rl, _ = alive_lock(tmp_path, Clock(), load=12.0, start_load=10.0)
        assert rl.run(Request(argv=["git", "push"], full=True, exclusive=True, max_wait_seconds=6)).exit_code == 75
    marked, cap, out = seen[0]
    assert (marked, cap, out.exit_code) == (1, 1, 75) and "the cap is 1 of 3 while a full run (--full, exclusive) waits for the load to fall" in out.reason
    assert not list(rl.p.load_waiting.iterdir()) and rl.fast_cap() == 3  # the marker goes with the request
