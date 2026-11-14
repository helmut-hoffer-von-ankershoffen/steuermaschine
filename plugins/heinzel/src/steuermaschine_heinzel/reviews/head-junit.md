# Review: `sm head junit` (new; bead bf3i)

Written by the builder; OPEN until an independent reviewer adds the verdict lines. Unit: `native/junit.py` (it imports
`native/lock.py` and `lock_lib/` through `lock`, and the plugin's `state.py`).

## What it does

A head whose selection is the whole Vitest suite (`web: ["web/ALL"]`, or `["ALL"]`) needs a full Vitest JUnit for
its packet; `sm gate run` refuses a Vitest run without a file filter unless the coordinator gives `--allow-full`.
`sm head junit WORKTREE --sel SEL.json --output FILE`:

1. refuses (exit 2) unless the selection's web lane is exactly the whole suite, or when the worktree has no
   `web/node_modules/.bin/vitest`;
2. writes one line to the gate's allow-full log (setting `lock_allow_full_log`, default `gate-allow-full.log` in the
   state directory) with the argv and the reason (`web/ALL selection SEL: one full Vitest JUnit run for the packet`);
3. runs `vitest run --maxWorkers=2 --reporter=default --reporter=junit --outputFile.junit=FILE` in WORKTREE/web under
   the run lock (`lock_of(allow_full=True)`: the same heavy slot, load bar and worker caps as `sm gate run`);
4. returns the exit code and the number of `<testcase>` elements written; a lock that gave up is exit 75.

## What to check

- The waiver is bound to a web/ALL selection file and to this one argv; nothing else runs with `allow_full`.
- `make pr` stays refused (`allow_full` never admits it; this command never runs make).

Tests: `plugins/heinzel/tests/test_native_head_junit.py`.

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: head junit
REVIEWED: sha256=c92c86afbb1c4ef8a5ab9851990f5fadcfb70e9de9c326d4df131b996877674c
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged since next-admission delta 4; new for it is gate lanes' rerun refusal in the plugin policy. X1 (advisory, cross-branch): a second `sm head junit` on an unchanged worktree after a failed Vitest run is now refused (exit 2, "the run lock refused the full Vitest run: ... to rerun anyway: --force-rerun REASON"), but `sm head junit` has no --force-rerun; the only route is `sm gate run --allow-full --force-rerun R -- <same argv>` from WORKTREE/web. Fails closed and matches the owner rule (no unchanged rerun of a failed check), so not blocking. Probe: ~/Code/packets/sm-reverdict-batch2/probe_rerun.py (heinzel lock.policy, real git, a vitest that exits 1: run 1 ran exit 1, run 2 refused exit 2). Fix in a follow-up: pass --force-rerun through, or name the real route in the refusal. Exit 75 on a lock that gave up and the waiver binding are unchanged. S11 (failed JUnit left at --output) still open.
COVERS: head junit
REVIEWED: sha256=6ee02f1b0f1e73a10ce906108515b467a859c9b8d10db29886031962d0444cdb
VERDICT: APPROVE
