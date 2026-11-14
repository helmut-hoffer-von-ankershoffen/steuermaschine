# Review: `sm queue hold` and `sm queue unhold` (new; bead gtco)

Written by the builder; OPEN until an independent reviewer adds the verdict lines (one `COVERS:` line per command:
`queue hold`, `queue unhold`). Unit: `native/queue_hold.py` (no library).

## What it does

- `sm queue hold --reason TEXT` writes one store record (kind `heinzel.queue-hold`, in the project's state
  directory): reason, since, host, pid. A second hold replaces the first and says so. An empty reason is a usage error.
- `sm queue unhold` empties the record and says what it lifted.
- `current()` reads it; `refuse_while_held(what)` raises a refusal naming the reason.

## Where it is read (other units; their digests move too)

- `sm daemon queue` (`native/queue.py`, units of `queue-add.md`, `queue-drop.md`, `daemon-queue.md`): while held, the
  call runs no tick at all (no merge in any repository, no main proof); it logs one line and returns `ran: false`,
  `held: REASON`. The moved queue code is unchanged.
- `sm queue why` (read): a first line `queue HOLD: ...`, the field `held`, the verdict `held: REASON`.
- `sm head ship` and `sm head pr --live` (`native/ship.py`, units of `head-ship.md`, `head-pr.md`): refuse with the
  reason before anything runs; `head pr` without `--live` still prints its plan.
- The release's and incidents' merge freeze (`merge-freeze`, `release-holds/`) is a separate mechanism and unchanged.

## What to check

- A held queue cannot merge through `sm daemon queue`; lifting the hold is the only way back besides deleting state.
- No path writes the hold except these two commands.

Tests: `plugins/heinzel/tests/test_native_queue_hold.py`.

## Re-verdict at feat/sm-next-admission 5fe179a (delta review 4, 2026-11-14)

Independent reviewer; wrote none of this code. Review: ~/Code/packets/sm-next-admission/REVIEW.md, delta review 4 (recorded verbatim by the integrator; COVERS from `self reviews`).

COVERS: queue hold
COVERS: queue unhold
REVIEWED: sha256=aa6b36a84a1fa3961d5f600c9968f4b5109c8a003f5ecbde4be59b603fb50ba8
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

next-admission (gtco, delta 4) only, plus config/protocols. queue_hold is registered (import list and MODULES). Matches the review file.
COVERS: queue hold
COVERS: queue unhold
REVIEWED: sha256=32433a28125fadf8286ee2d13c7da02758e12b4ffcc3b5084d290dc73988f040
VERDICT: APPROVE
