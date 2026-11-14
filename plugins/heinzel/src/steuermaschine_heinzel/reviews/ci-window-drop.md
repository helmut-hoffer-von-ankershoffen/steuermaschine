# Review: `sm ci window drop` (new) and the dead-waiter prune of the validation window (bead b5vp)

Written by the builder; OPEN until an independent reviewer adds the verdict lines. Same unit as `head-validate.md`
(`native/window.py`, `native/dispatch.py`, `native/dispatch_lib/`, `native/lock.py`, `native/lock_lib/`): every
change listed here also voids that file's verdict.

## What changed in the unit

1. `PrunedWindow` (window.py): the plugin's window subclasses the core's `Window` and drops, in `_expire`, every
   waiting turn whose command is known to be gone (`process_alive` is False: same host, pid gone). `_expire` runs in
   `enter` (saved) and in `status` (shown), so a dead waiter no longer blocks the heads behind it for
   `window_waiting_minutes`. A waiter on another machine (liveness None) is kept, as before. The core is unchanged.
2. `liveness(turn)`: the core's `process_alive`, a seam the tests replace.
3. `sm ci window drop HEAD [--worktree DIR]`: HEAD is a branch, a full SHA or a SHA prefix of 7+ hex characters.
   - the window's holder: refused (`sm ci window release` is the route); no match: refused;
   - a waiter on another machine: refused (cannot be checked or stopped from here);
   - a waiter whose command is gone: dropped for anyone (`Window.leave` with that turn's owner token);
   - a waiter whose command runs: only with `--worktree` at exactly that head (owner proof); its pid gets SIGTERM
     (`stop_process`, which `validate` turns into its normal cleanup), then the turn is left. A pid that ended in
     between is noted and the turn is still dropped.

## What to check

- Nothing here can end another builder's validation that holds the window, or stop a process for a head the caller
  does not have checked out.
- The SIGTERM goes to the recorded pid of a turn on this host only.

Tests: `plugins/heinzel/tests/test_native_window_waiters.py`.

## Also in this unit: detached validations (bead p0hc)

`sm head validate WORKTREE --detach` runs the same checks as `--dry-run`, then starts the same command
(`sm --format json head validate WORKTREE --lanes ... --os ... --max-wait ... [--serial]`) in the background through
the process runner (`/bin/sh -c 'nohup ... &'`, stdout to `validations/HANDLE.json`, stderr to `HANDLE.log` in the
project's state directory), records `HANDLE.meta.json` (pid, host, head) and returns the handle at once.
`sm head validate --wait HANDLE [--max-wait MIN]` (default 8 minutes, under an agent's 10-minute tool limit) returns
the background command's result with its exit code, re-raises its refusal or wait, exits 75 while it still runs, and
refuses when its process is gone without a result. `--max-wait` without `--wait` keeps its 60-minute default.

Check: the background command is the same write command under the same gate (it runs through `sm`, so its own review
gate applies); nothing is pushed or dispatched by the `--detach` call itself. Tests:
`plugins/heinzel/tests/test_native_window_detached.py`.

## Also in this unit: the release-window lock (moves the unit of `release-admit-apply.md` too)

New module `native/release_window.py` (one store record, kind `heinzel.release-window`: step, packet, pid, host,
since, until). `sm release admit prepare` takes it for `release_window_minutes` (default 60, a setting of the window
mixin) and leaves it in place for the review and the apply, also when it refuses (so active runs can drain);
`sm release admit apply` takes it again and gives it back when the registry write succeeded (a failed apply keeps it).
`sm head validate` checks it before every look at the validation window and exits 75 while it holds, before any push
or dispatch. A record past its time is no lock. `release_admit.py` changed only by the two `with` blocks and the help.

Check: the lock can only delay validations (exit 75), never let one through that the window would hold; nothing in
the release route reads it. Tests: `plugins/heinzel/tests/test_native_release_window.py`.

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: ci window drop
REVIEWED: sha256=27e8c300945b7e10f8091e990d7a32f14be36cbebc561e7007139dfa3019776c
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission p0hc/b5vp (detached validate, exit 1 for a finished waiting/incomplete child, drop bound to worktree and start time, release-window wait) plus gate lanes' lock.py: the push runs `git ... push`, which is never refused as a rerun and never learned light by a unique SHA argv. Composition adds nothing. S7 (drop TOCTOU) still open.
COVERS: ci window drop
REVIEWED: sha256=db1a83dd655a5781a65af6b87909c836a705caceddb76f7daefd8509b9785454
VERDICT: APPROVE

## Re-verdict at 5b48447: window admission merged onto integration/sm-batch2 (2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-window-admission/REVIEW.md, round 3 (delta 8927d34..5b48447: admission hold, release-window order, drop of gone waiters, release-hold; withdraw and PrunedWindow removed). Shared core changed by integration/sm-batch2 (runlock, usage log, gate) is covered by its own reviews, not by this one.

COVERS: ci window drop
REVIEWED: sha256=f42d33d9bf73be9c76d4acd4e69dbeaa4d9b01cf4917b89235af1c63bb267148
VERDICT: APPROVE
