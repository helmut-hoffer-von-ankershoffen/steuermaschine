# Review: `sm head slots apply` (and its dry run `sm head slots collect`)

For the independent reviewer. `head slots apply` is the one command here that writes: up to five GitHub repository
variables, the focused plan slots of the registry that decides which tests protect main. It is unavailable until this
file ends with the verdict line; the author does not write that line. Origin: bead heinzel-management-pzos (PLANS at
97 %, V2_0 and V2_1 at 81 % on 2026-11-14; nothing removed an entry of a merged head).

Unit: `native/slots.py` plus `native/slots_lib/` (`collect.py` pure functions, `legacy.py` read-only receipt reader).
Nothing is moved from an old tool and no moved library is edited: the code is new and imports nothing of `admission_lib`.

## 1. The pattern: prepare, digest, review, apply

- `sm head slots collect` (`writes=False`, a dry run) reads the five variables, every pull request (`gh pr list --state all`,
  1000 newest) and the release reservation receipts, and prints the rows and `transaction`: per slot the keys removed and
  the sha256 of the value before and after. `digest` is the sha256 of that document in canonical JSON.
- The reviewer reads `sm head slots collect --all --format json`, checks each row to collect against its pull request, and
  writes the digest into the verdict (`VERDICT: APPROVE`; the digest is the thing approved).
- `sm head slots apply --approved DIGEST` rebuilds the transaction from fresh reads and refuses unless its digest is the
  approved one. It is a separate command, not an option of `collect`, because a command with one writing option is split
  (INTEGRATION 8.1); `collect` therefore stays usable before any verdict exists.

## 2. What may be collected (`collect.classify`)

1. An entry `pull_request:<head>` is a candidate only when at least one pull request has that head, none is open, and
   every closed one was closed more than 7 days ago (`CLOSED_GRACE_SECONDS`; close and reopen is a standard loop and must find
   its plan; an unreadable close time counts as recent). Merged pull requests have no grace. A head no pull request has any
   more (updated, or older than the 1000 listed) is `unmatched` and stays.
2. A candidate stays when its tree is the tree of an open, recent or unmatched pull request entry, of a release reservation
   whose version is not a published release (releases unreadable: every reservation holds), or of main's head read now
   (`gh api commits/main`): a rerun of main's CI looks up `push-tree:<that tree>`. Main's sha and tree are in the transaction,
   so the digest binds them, and apply reads main again right before the first write.
3. An entry `push-tree:<tree>` goes only together with a collected pull request entry of that tree and when nothing wants
   the tree. One with no pull request entry stays (it may be main's own).
4. Unreadable or undecodable slots, an unreadable pull request list or main: refusal, nothing collected.

## 3. Guards of the write

| Guard | Where | Test |
|---|---|---|
| exact approved digest, from fresh reads | `head_slots_apply` | `test_apply_needs_the_exact_approved_digest...` |
| no Actions run active (exit 75) | `forge.active_runs()` | `test_apply_waits_while_an_actions_run_is_active...` |
| the lock `head apply` takes: `admission_lib.focused_admit._lock` at `<state>/focused-pr-registry.lock` (`slots_lib/registry_lock.py`) | `registry_lock.writer_lock` | `test_apply_takes_the_lock_head_apply_takes...` |
| all five variables read again right before each write and equal to what this run expects; main read again before the first | `head_slots_apply` | `test_a_writer_that_appends_after_the_read_is_never_overwritten`, `test_apply_refuses_when_main_moved...` |
| a writer that finished before the apply keeps its entry (the digest changes, apply refuses, a new collect keeps it) | digest | `test_a_writer_that_finished_before_the_apply_keeps_its_entry` |
| readback equals what was written, else stop (no automatic restore) | after the last write | `test_apply_stops_when_the_readback_differs` |
| a stop after the first write records a receipt with `partial: true` and the slots written | `record(True)` | `test_a_partial_write_records_a_receipt...` |
| a slot that loses every entry is written as the envelope of `{}`, never as an empty value | `collect.encode` | `test_a_slot_that_loses_every_entry...` |
| only slots that lose entries are written; every removed key must still be there | `collect.new_values` | `test_new_values_refuses_a_key_that_is_gone...` |
| receipt, write once, keyed by the digest | `store.put_receipt` | `test_apply_needs_the_exact_approved_digest...` |

## 4. What the reviewer should look at, and what is NOT verified

- Format: the compressed slot is rewritten as `{"schema":1,"codec":"gzip+base64","bytes","sha256","data"}` (gzip, no time
  stamp), the shape `admission_lib/focused_slots.registry_text` reads and the tests of the stuck-queued receipt build. The
  Core reader's own acceptance of a rewritten slot is not verified here; the dry run on the live registry first.
- The queue is not paused (`head apply` pauses it). Open and queued pull requests keep their entries and main's tree is kept,
  so a reader sees only removals of entries of merged or long-closed heads. Say if the queue must be paused anyway.
- The reservation reader reads receipts of the admission receipt directory; a reservation recorded anywhere else is not seen.
- Not run against the live registry: tests use a fake forge and a scripted `gh` only.

## Independent review: d94de45

Two rounds (REVIEW.md: REJECT R1-R4 at e6db9d9; REVIEW-2.md at d94de45). R3 same lock file as head apply (probed: excluded,
Busy after 15 s); R4 main tree kept, main sha+tree in the digest, "main moved" refused; closed grace 7 days, unknown = recent;
empty slot is the envelope of {} (Core decode_slot reads it, and a rewritten slot); no queue pause needed.

COVERS: head slots apply
REVIEWED: sha256=142f61b54c077c66aa5713104b841077301a5d20f792590ba50a4699f13fc233
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

New unit (gate lanes R3/R4): takes the same registry lock as head apply, binds main sha+tree. Composition with next-admission: an m598 PR-head update keeps the old head's entry while the PR is open (state open -> wanted), and the release reservation trees stay wanted; recut changes only how the reservation packet is built, not the receipt slots reads. slots is registered in native/__init__.py.
COVERS: head slots apply
REVIEWED: sha256=aa530378a3d71d895471eb560a3a1ca5829071360634592a660c73aadf380adb
VERDICT: APPROVE
