"""bin/promote's switch decisions (bin/promote_lib.sh), run in bash with a fake `ps` on PATH and a temporary stable dir.

- a running admission or release step is waited for (bounded), then the switch goes on; still running: refused
- the quality gate's receipt is per commit: a retry of the same candidate does not repeat the gate
"""

from __future__ import annotations

import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "bin" / "promote_lib.sh"
PROMOTE = LIB.with_name("promote")


def bash(script: str, tmp_path: Path, ps_lines: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run `script` after sourcing the library. `ps_lines`: one listing per `ps` call (the last one repeats)."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    listings = ps_lines or [""]
    for number, text in enumerate(listings):
        (fake / f"ps.{number}").write_text(text)
    (fake / "ps").write_text(
        "#!/bin/bash\n"
        f'n="$(cat "{fake}/count" 2>/dev/null || echo 0)"; echo $((n + 1)) > "{fake}/count"\n'
        f'[ -f "{fake}/ps.$n" ] || n={len(listings) - 1}\n'
        f'cat "{fake}/ps.$n"\n')
    (fake / "ps").chmod(0o755)
    return subprocess.run(["bash", "-c", f'. "{LIB}"; {script}'], capture_output=True, check=False, text=True, timeout=60,
                          env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(tmp_path)})


APPLY = "4242 /x/venv/bin/python /x/venv/bin/steuermaschine head apply prepare.out\n"


def test_no_running_step_switches_at_once(tmp_path):
    done = bash("wait_idle 60 0; echo rc=$?", tmp_path, ["123 /bin/zsh\n"])
    assert done.stdout.strip() == "rc=0" and "waiting" not in done.stderr


def test_a_running_step_is_waited_for_and_then_the_switch_goes_on(tmp_path):
    done = bash("wait_idle 60 0; echo rc=$?", tmp_path, [APPLY, APPLY, "123 /bin/zsh\n"])
    assert done.stdout.strip() == "rc=0"
    assert "waiting up to 60s" in done.stderr and "head apply" in done.stderr and "ended after" in done.stderr


def test_a_step_still_running_after_the_bound_refuses_and_names_it(tmp_path):
    done = bash("wait_idle 0 0; echo rc=$?", tmp_path, [APPLY])
    assert done.stdout.splitlines() == [APPLY.strip(), "rc=1"]


def test_read_commands_and_the_grep_itself_are_not_steps(tmp_path):
    listing = "1 /x/venv/bin/steuermaschine head size wt\n2 grep -E head apply\n3 sm release status\n"
    assert bash("busy_steps", tmp_path, [listing]).stdout == ""
    assert "release admit" in bash("busy_steps", tmp_path, ["5 /s/bin/sm release admit 0.9.41\n"]).stdout
    assert "release cut" in bash("busy_steps", tmp_path, ["6 /s/bin/sm release cut 0.9.42 --live\n"]).stdout  # a pipeline


def test_the_gate_receipt_is_per_commit(tmp_path):
    stable = tmp_path / "stable"
    done = bash(f'gate_passed "{stable}" abc && echo before; record_gate "{stable}" abc; gate_passed "{stable}" abc && echo after;'
                f' gate_passed "{stable}" def || echo other', tmp_path)
    assert done.stdout.split() == ["after", "other"]
    assert (stable / "gate-passed" / "abc").read_text().strip().endswith("Z")


def test_promote_sources_the_library_and_offers_the_new_options():
    text = PROMOTE.read_text()
    assert '. "$here/bin/promote_lib.sh"' in text and "wait_idle $((wait_min * 60))" in text
    assert "gate_passed \"$stable\" \"$sha\"" in text and "record_gate \"$stable\" \"$sha\"" in text
    help_text = subprocess.run(["bash", str(PROMOTE), "--help"], capture_output=True, check=False, text=True, timeout=30).stdout
    assert "--wait-idle MIN" in help_text and "--recheck" in help_text


def test_the_gate_receipt_is_written_only_after_head_and_tree_are_checked_unchanged():
    text = PROMOTE.read_text()
    moved = text.index("the checkout moved while the gate ran")
    assert text.index('record_gate "$stable" "$sha"') > moved  # a commit landing during the gate gets no receipt
    assert 'sha="$full"' in text and 'git archive "$full"' in text  # the checked commit is the one built


def test_an_option_without_its_value_is_refused():
    for option in ("--wait-idle", "--red-gate-ok"):
        done = subprocess.run(["bash", str(PROMOTE), option], capture_output=True, check=False, text=True, timeout=30)
        assert done.returncode == 2 and "takes" in done.stderr, option


def test_a_refused_gate_names_the_failing_tests(tmp_path):
    out = tmp_path / "check.out"
    out.write_text("check: FAIL tests\n    =========== short test summary info ===========\n"
                   "    FAILED plugins/heinzel/tests/test_a.py::test_one - AssertionError\n"
                   "    ERROR examples/layer/tests/test_layer.py::test_two\ncheck: PASS typos\n")
    assert bash(f'failed_tests "{out}"', tmp_path).stdout.splitlines() == [
        "FAILED plugins/heinzel/tests/test_a.py::test_one", "ERROR examples/layer/tests/test_layer.py::test_two"]
    assert 'failed_tests "$summary"' in PROMOTE.read_text()


def test_the_switch_pending_file_names_this_promote_and_only_it_removes_it(tmp_path):
    pending = tmp_path / ".cache" / "sm-promote-switch-pending"
    done = bash('mark_switch_pending; echo "$$"; cat "$(pending_file)"; clear_switch_pending; [ -f "$(pending_file)" ] || echo gone', tmp_path)
    me, written, gone = done.stdout.splitlines()
    assert written.split()[0] == me and gone == "gone"
    pending.write_text("1 other\n")  # another promote's file
    bash("clear_switch_pending", tmp_path)
    assert pending.read_text() == "1 other\n"
    text = PROMOTE.read_text()
    assert text.index("mark_switch_pending") < text.index("wait_idle $((") and "trap clear_switch_pending EXIT" in text


def test_a_red_gate_let_through_records_its_reason_next_to_the_receipts(tmp_path):
    stable = tmp_path / "stable"
    done = bash(f'record_red_gate "{stable}" abc "plugin coverage is a known item" "coverage plugin >= 90"; red_gate_log "{stable}";'
                f' gate_passed "{stable}" abc || echo no-receipt', tmp_path)
    log = stable / "gate-red-accepted"
    assert done.stdout.split() == [str(log), "no-receipt"]  # evidence only: a retry still runs the gate
    line = log.read_text().strip()
    assert line.endswith(" abc reason=plugin coverage is a known item red=coverage plugin >= 90") and line[:20].endswith("Z")


def test_promote_records_the_red_gate_reason_and_names_it_in_its_log():
    text = PROMOTE.read_text()
    accepted = text.index("RED GATE ACCEPTED: $redok")
    assert text.index('record_red_gate "$stable" "$sha" "$redok"') > accepted and "reason recorded in $(red_gate_log" in text


def release(tmp_path: Path, name: str, knows: bool = True, fails: str = "") -> Path:
    """A fake `steuermaschine` of one release: logs every call (with its user config) and knows the state moves or not."""
    program = tmp_path / name / "steuermaschine"
    program.parent.mkdir(parents=True)
    program.write_text(
        "#!/bin/bash\n"
        f'echo "{name} $STEUERMASCHINE_USER_CONFIG $*" >> "{tmp_path}/calls"\n'
        + ("" if knows else 'case "$*" in *migrate-state*) exit 2 ;; esac\n')
        + 'case "$*" in *--help) exit 0 ;; esac\n'
        + (f'case "$*" in "{fails}"|"{fails} "*) exit 70 ;; esac\n' if fails else "")
        + 'case "$*" in *--list*) echo \'{"moves": [{"old": "/o/q"}]}\' ;; esac\n')
    program.chmod(0o755)
    return program


def calls(tmp_path: Path) -> list[str]:
    return [line.split(" ", 2)[0] + " " + line.split(" ", 2)[2].replace(" --help", "?") for line in (tmp_path / "calls").read_text().splitlines()]


def test_the_target_moves_forward_after_the_live_release_moved_back_what_only_it_knows(tmp_path):
    new, old = release(tmp_path, "new"), release(tmp_path, "old")
    done = bash(f'migrate_state_to "{new}" new.toml "{old}" old.toml; echo rc=$?', tmp_path)
    assert done.stdout.strip().endswith("rc=0"), done.stderr
    assert calls(tmp_path) == ["new self migrate-state?", "new --format json self migrate-state --list", "old self migrate-state?",
                               f"old self migrate-state --back --keep {calls(tmp_path)[3].rsplit(' ', 1)[1]}", "new self migrate-state?", "new self migrate-state"]
    assert all(line.split()[1] == f"{line.split()[0]}.toml" for line in (tmp_path / "calls").read_text().splitlines())  # each with its own config


def test_a_target_that_knows_no_moves_only_moves_the_live_release_back(tmp_path):
    old, new = release(tmp_path, "old", knows=False), release(tmp_path, "new")
    done = bash(f'migrate_state_to "{old}" old.toml "{new}" new.toml; echo rc=$?', tmp_path)
    assert done.stdout.strip().endswith("rc=0")
    assert [c for c in calls(tmp_path) if not c.endswith("?")] == [calls(tmp_path)[2]] and "--back --keep" in calls(tmp_path)[2]


def test_a_refused_move_stops_the_switch(tmp_path):
    new = release(tmp_path, "new", fails="self migrate-state")
    done = bash(f'migrate_state_to "{new}" new.toml; echo rc=$?', tmp_path)
    assert done.stdout.strip().endswith("rc=1")
    new2 = release(tmp_path, "new2", fails="self migrate-state --back --keep")
    old = release(tmp_path, "old2")
    done = bash(f'migrate_state_to "{old}" old.toml "{new2}" new.toml; echo rc=$?', tmp_path)
    assert done.stdout.strip().endswith("rc=1")


def test_promote_and_rollback_migrate_the_state_before_they_switch():
    promote, rollback = PROMOTE.read_text(), PROMOTE.with_name("rollback").read_text()
    assert promote.index("migrate_state_to") < promote.index('say "5/5 switch"') and promote.index("wait_idle $((wait_min") < promote.index("migrate_state_to")
    assert rollback.index("wait_idle") < rollback.index("migrate_state_to") < rollback.index("python3 -")
