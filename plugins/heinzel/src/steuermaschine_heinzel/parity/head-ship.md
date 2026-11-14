# Parity record: shipping one head, old tools against native commands

Recorded 2026-11-12 with `python3 plugins/heinzel/port/parity.py OLD_TOOLS_DIR` (all 40 scenarios of the harness the
same; the 15 (now 17) of this unit are below, the queue's 23 are in [queue-add.md](queue-add.md), which also describes the
method and the declared differences N1 to N6). Old tools: the working tree of the management checkout of that
evening (git 170e6f3 plus its uncommitted edits). Re-run 2026-11-13 after the rebase on main and the owner's
decisions (no auto-merge, texts check, combine-close dropped), against the management checkout's tools of that day:
the 15 scenarios below the same. Re-run 2026-11-13 after the review fixes (B1: `--fix-forward` passes
`--reviewed-head` and a REASON to the lane): 17 scenarios, the two new fix-forward ones included, the same.

Old side per scenario: `hz-ship` (the shell script, with the other tools on its PATH as it expects), `hz-diff-guard`,
`hz-push-gate`. Compared: exit code, stdout, stderr, the calls in order with their
arguments (the transcript), the queue state file, the notes document, the alarm state, the interventions log.

Declared differences beyond N1 to N6: the names of this unit in hints (`hz-ship` -> `sm head ship`, `hz-push-gate N`
-> `sm head push N`, `hz-wait` -> `sm go`, `hz-gate` -> `sm gate run`); the minutes in the gate's lines; in `ship-usage`
the old side is `hz-ship 801 --reviewed-head` (no value) and the native side gives the option a value without a
reason: both are the same usage error, exit 2.

Side effects, identical on both sides: the ship scenarios write only the queue state file and, with a reason, one
note. No other write. `head combine-close` is not in this unit any more (the coord slice's command is the one).

Not compared: `sm head pr` (the old route ran the repository's `make pr`, which must not run on this machine; the
native route is new code, tested in tests/test_native_ship_commands.py), `sm head push` with the CI meta preflight
(the preflight area's, tested there).

## Result

### ship-pass: SAME
- old `hz-ship 801`, native `sm head ship 801`; exit 0 / 0; 4 / 4 calls; writes to the forge: none
### ship-flagged-needs-review: SAME
- old `hz-ship 801`, native `sm head ship 801`; exit 2 / 2; 2 / 2 calls; writes to the forge: none
### ship-reviewed: SAME
- old `hz-ship 801 independent review: research/review-pr801.md`, native `sm head ship 801 independent review: research/review-pr801.md`; exit 0 / 0; 2 / 2 calls; writes to the forge: none
### ship-reviewed-head: SAME
- old `hz-ship 801 review r --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828`, native `sm head ship 801 review r --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828`; exit 0 / 0; 3 / 3 calls; writes to the forge: none
### ship-reviewed-head-moved: SAME
- old `hz-ship 801 review r --reviewed-head ffffffffffffffffffffffffffffffffffffffff`, native `sm head ship 801 review r --reviewed-head ffffffffffffffffffffffffffffffffffffffff`; exit 1 / 1; 3 / 3 calls; writes to the forge: none
### ship-cleared: SAME
- old `hz-ship 801`, native `sm head ship 801`; exit 0 / 0; 0 / 0 calls; writes to the forge: none
### ship-usage: SAME
- old `hz-ship 801 --reviewed-head`, native `sm head ship 801 --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828`; exit 2 / 2; 0 / 0 calls; writes to the forge: none
### guard-pass: SAME
- old `hz-diff-guard 801`, native `sm head guard 801`; exit 0 / 0; 1 / 1 calls; writes to the forge: none
### guard-flag: SAME
- old `hz-diff-guard 801`, native `sm head guard 801`; exit 1 / 1; 1 / 1 calls; writes to the forge: none
### guard-unreadable: SAME
- old `hz-diff-guard plugins 38`, native `sm head guard plugins 38`; exit 2 / 2; 1 / 1 calls; writes to the forge: none
### push-gate-ok: SAME
- old `hz-push-gate 801`, native `sm head push 801 --gate-only`; exit 0 / 0; 2 / 2 calls; writes to the forge: none
### push-gate-wait-main-starved: SAME
- old `hz-push-gate 801`, native `sm head push 801 --gate-only`; exit 75 / 75; 7 / 7 calls; writes to the forge: none
### push-gate-wait-release-candidate: SAME
- old `hz-push-gate 801`, native `sm head push 801 --gate-only`; exit 75 / 75; 4 / 4 calls; writes to the forge: none
### push-gate-waiver: SAME
- old `hz-push-gate 801 --min 15`, native `sm head push 801 --min 15`; exit 0 / 0; 4 / 4 calls; writes to the forge: none
### push-gate-github-down: SAME
- old `hz-push-gate fix/x`, native `sm head push fix/x --gate-only`; exit 0 / 0; 1 / 1 calls; writes to the forge: none
### ship-fix-forward-reviewed-head: SAME
- old `hz-ship 801 --fix-forward --for web-2=9kk --reviewed r --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828`, native `sm head ship 801 --fix-forward --for web-2=9kk --reviewed r --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828`; exit 1 / 1; 0 / 0 calls; writes to the forge: none
### ship-fix-forward-reason: SAME
- old `hz-ship 801 --fix-forward --for web-2=9kk review r`, native `sm head ship 801 --fix-forward --for web-2=9kk review r`; exit 1 / 1; 0 / 0 calls; writes to the forge: none
17 of 17 scenarios the same
