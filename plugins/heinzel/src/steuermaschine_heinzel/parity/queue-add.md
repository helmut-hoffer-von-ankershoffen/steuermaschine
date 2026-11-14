# Parity record: the merge queue, old tool against native command

Recorded 2026-11-12 at commit 5441640 with `python3 plugins/heinzel/port/parity.py OLD_TOOLS_DIR` (old tools: the working tree of the
management checkout of that evening, git 170e6f3 plus its uncommitted edits). Run it again to reproduce; add `--show`
to print both sides in full.

Each scenario runs the old tool and the native command in separate throwaway HOME directories with the same state file
(a copy of the live queue state of 2026-11-12, last 25 rows, plus the scenario's rows), the same scripted `gh`
(port/fakebin/tool.py, first on PATH for both sides, so the comparison does not depend on the injected runner; the
native side is started by port/native_cli.py, which sets the test bypass of the review gate and runs nowhere else), and
compares: exit code, stdout, stderr, the calls in order with their arguments and stdin (the transcript), the state
file afterwards, the notes document, the alarm state file, the queue log, and that the lock file exists.

Different on purpose (applied before comparing, nothing else is ignored):

- N1 the command name in printed hints and log lines (`hz-queue` -> `sm queue`; `hz-queue run started` -> `queue run started`).
  Notes keep their old wording (`hz-queue: batch: ...`): the notes document is read by people and other tools.
- N2 times (log and note stamps, epoch fields of the state file, tick duration, process id). N3 the HOME path.
- N4 `drop` was silent; native prints `dropped KEY` or `not in the queue: KEY` per PR.
- N5 a command that prints nothing on stdout prints one empty line.
- N6 a usage error (exit 2 on both sides) has the text of `sm`, not of the old argument parser.

Side effects of the write scenarios, in order (identical on both sides): see the `writes to the forge` field; the only
write to GitHub in these scenarios is `gh pr merge PR --squash --match-head-commit HEAD` in `tick-merge` and
`tick-merge-refused-by-forge`, after the reads `rulesets` x3, `run list`, `pr view` (assess), `rules/branches/main`,
`run list` (main state), `pr view files`, `pr view` (state, head), `rules/branches/main`, `pr view statusCheckRollup`,
`pr view headRefOid,mergeStateStatus,statusCheckRollup`, and followed by `pr view mergeCommit` x2.

Not covered here (covered by tests in tests/test_native_queue_commands.py and test_native_queue_edge.py instead):
the bead stamp at merge (the old stamp script puts /opt/homebrew/bin before PATH, so a black-box run would reach the
real `gh` and `bd`), `--fix-forward`, `main-rerun`, `checkpoint`, a batch of several PRs with the main batch proof,
and the loop over several ticks (`--loop`).

## Result

### list: SAME
- old `hz-queue list`, native `sm queue list`; exit 0 / 0; 0 / 0 calls; writes to the forge: none
### list-active: SAME
- old `hz-queue list --active`, native `sm queue list --active`; exit 0 / 0; 0 / 0 calls; writes to the forge: none
### why: SAME
- old `hz-queue why`, native `sm queue why`; exit 0 / 0; 3 / 3 calls; writes to the forge: none
### add-pass: SAME
- old `hz-queue add 801`, native `sm queue add 801`; exit 0 / 0; 2 / 2 calls; writes to the forge: none
### add-after-bead: SAME
- old `hz-queue add --after 796 --bead abc 801`, native `sm queue add --after 796 --bead abc 801`; exit 0 / 0; 2 / 2 calls; writes to the forge: none
### add-flagged-refused: SAME
- old `hz-queue add 801`, native `sm queue add 801`; exit 1 / 1; 1 / 1 calls; writes to the forge: none
### add-flagged-reviewed: SAME
- old `hz-queue add --reviewed review: research/review-pr801.md 801`, native `sm queue add --reviewed review: research/review-pr801.md 801`; exit 0 / 0; 2 / 2 calls; writes to the forge: none
### add-reviewed-head: SAME
- old `hz-queue add --reviewed review: research/review-pr801.md --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828 801`, native `sm queue add --reviewed review: research/review-pr801.md --reviewed-head e9c943810ea75cfad155a431a2f168b2a9559828 801`; exit 0 / 0; 3 / 3 calls; writes to the forge: none
### add-reviewed-head-moved: SAME
- old `hz-queue add --reviewed r --reviewed-head ffffffffffffffffffffffffffffffffffffffff 801`, native `sm queue add --reviewed r --reviewed-head ffffffffffffffffffffffffffffffffffffffff 801`; exit 1 / 1; 3 / 3 calls; writes to the forge: none
### add-reviewed-head-empty: SAME
- old `hz-queue add --reviewed r --reviewed-head  801`, native `sm queue add --reviewed r --reviewed-head  801`; exit 1 / 1; 0 / 0 calls; writes to the forge: none
### add-diagnostics-empty: SAME
- old `hz-queue add --diagnostics-main  801`, native `sm queue add --diagnostics-main  801`; exit 1 / 1; 0 / 0 calls; writes to the forge: none
### add-mitigation-evidence-empty: SAME
- old `hz-queue add --mitigation-evidence  801`, native `sm queue add --mitigation-evidence  801`; exit 1 / 1; 0 / 0 calls; writes to the forge: none
### add-two-kinds: SAME
- old `hz-queue add --fixes-main --fix-forward 801`, native `sm queue add --fixes-main --fix-forward 801`; exit 2 / 2; 0 / 0 calls; writes to the forge: none
### add-other-repository: SAME
- old `hz-queue add plugins 38 801`, native `sm queue add plugins 38 801`; exit 0 / 0; 4 / 4 calls; writes to the forge: none
### add-fixes-main: SAME
- old `hz-queue add --fixes-main 801`, native `sm queue add --fixes-main 801`; exit 0 / 0; 2 / 2 calls; writes to the forge: none
### add-unreadable-diff: SAME
- old `hz-queue add 801`, native `sm queue add 801`; exit 1 / 1; 1 / 1 calls; writes to the forge: none
### drop: SAME
- old `hz-queue drop 801 796`, native `sm queue drop 801 796`; exit 0 / 0; 0 / 0 calls; writes to the forge: none
### tick-nothing-queued: SAME
- old `hz-queue run --once`, native `sm daemon queue`; exit 0 / 0; 4 / 4 calls; writes to the forge: none
### tick-merge: SAME
- old `hz-queue run --once`, native `sm daemon queue`; exit 0 / 0; 15 / 15 calls; writes to the forge: ['pr merge 801 -R helmut-hoffer-von-ankershoffen/heinzel --squash --match-head-commit e9c943810ea75cfad155a431a2f168b2a9559828']
### tick-merge-refused-by-forge: SAME
- old `hz-queue run --once`, native `sm daemon queue`; exit 0 / 0; 14 / 14 calls; writes to the forge: ['pr merge 801 -R helmut-hoffer-von-ankershoffen/heinzel --squash --match-head-commit e9c943810ea75cfad155a431a2f168b2a9559828']
### tick-waits-on-freeze: SAME
- old `hz-queue run --once`, native `sm daemon queue`; exit 0 / 0; 6 / 6 calls; writes to the forge: none
### tick-reviewed-head-moved: SAME
- old `hz-queue run --once`, native `sm daemon queue`; exit 0 / 0; 5 / 5 calls; writes to the forge: none
### tick-blocked-with-green-checks: SAME
- old `hz-queue run --once`, native `sm daemon queue`; exit 0 / 0; 6 / 6 calls; writes to the forge: none

23 of 23 scenarios the same
