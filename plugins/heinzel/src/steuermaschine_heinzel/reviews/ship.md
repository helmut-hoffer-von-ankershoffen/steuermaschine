# Review: `sm ship` (driver over `sm head admit`)

Unit: `native/ship_driver.py` and every first-party file it imports, including core and the `head admit` pipeline
(`sm self reviews` lists them and the digest). `sm ship` adds no step of its own: the steps are those of the pipeline,
each its own unit with its own gate, checked again when the pipeline calls it.

What it writes: the selection file `ship/sel/BRANCH.json` and the directory `ship/reviews` in the plugin state
directory; through `head admit`: its run state, preflight summary, packet files, prepare.out; with --live: the plan
slot (head apply), one push and the PR (head pr), one queue entry (queue add). `--wait` only reads (the PR through
`gh pr view`, the queue through `queue list`, the clock through the provider).

What the reviewer checks: the pipeline's own review gate is checked before anything runs; the derived inputs
(`core_repo` from the setting, refused naming it when unset; `review_root`; `sel` from `head select` against
origin/main, python and web lanes, refused when the selection is full or empty) are used only when the input is
neither given with --set nor remembered by an earlier run, and never replace one; the inputs are passed to the pipeline
unchanged; no machine path is a default; both review stops of `head admit` stay (a verdict is not derived here);
`--wait` ends with exit 0 only for a merged PR, exit 1 for a closed PR or a dropped or needs-work queue entry, exit 75
at the ceiling, and it never writes or retries a step; the clock comes from the provider.

State: open. Waiting for an independent review.

COVERS: ship

## Delta re-review at d05ef01 (against e09b361, 2026-11-14)

Independent reviewer (Opus), not the builder. Delta: 5 files (guard_floor.py, bin/promote_lib.sh, ship_driver.py and
two test files). The ship, guards floor and promote script tests pass through `sm gate run`.

R1 fixed: `ship` is a one-word step in `guard_floor.WRITE_STEPS` (`sm_step` returns `("ship",)` only for a word in
ONE_WORD_STEPS) and `ship( |$)` is in `busy_steps`. The equality test reads the third alternative. The probe is now a
test (`sm ship`, `--format json ship`, `gate run -- sm ship`), and `sm head ship 801` and `sm shipping` still run
during a switch. R2 accepted as built: the wrapper keeps the writers' lock, but each poll checks `switch_pending` and
ends with exit 75, so a promote waits at most one poll (60 s) plus one read, well inside its 30-minute `wait_idle`.
A1: e2e lanes now refuse with `--set sel=`. A2: a finished run without a PR refuses. A3: the file name carries the
head. A4: an unreadable PR with no queue entry is "unknown" and keeps waiting. Each has a test.

COVERS: ship
REVIEWED: sha256=72691029a8c02be750f262604d40ec60484037f64c18adaa1fe00c9ec894a992
VERDICT: APPROVE
