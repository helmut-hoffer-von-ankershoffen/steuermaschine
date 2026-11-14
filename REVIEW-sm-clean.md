# Independent review: chore/sm-clean at 6acd0b4 (+ f39eccd)

Reviewer wrote none of the reviewed code. Scope: the 8 pushed commits 143a671..6acd0b4 on merge base f9134cc.
During the review the author pushed f39eccd (child environments through config.py `child_environment`/`path_with`;
mini.py, packet.py, debt.json, test_core_settings.py). Read but not reviewed in depth; it touches no stale review unit
and `sm self reviews` is unchanged by it. The bug in section 2 is still present at f39eccd.

## 1. Stale review units: help text only (verdicts re-issued)

`sm self reviews` listed four units stale: beads-agents-add (8aba0e9e…), head-ship and head-pr (549a3c6e…),
beads-tasks (f1fa62d4…). For each, the digest recomputed over the unit files at f9134cc equals the old `REVIEWED:`
value (cd70ce1b…, 9cf87db5…, ca416f17…), so main was approved and only this branch moved them.
`git diff f9134cc 6acd0b4` over all unit files touches three files, `help=` keyword only:

- native/agents.py: BEAD, AGENT_ID, MODEL arguments of `beads_agents_add`
- native/ship.py: PR argument of `head_ship`
- native/tasks.py: TITLE (add), TEXT (note), AGENT (claim), PR and STATE (pr)

No default, type, metavar, option name or body line changed. Verdicts re-issued in the four review files with the
note "help text only since the previous verdict"; `sm self reviews` now shows 0 stale (43 approved, 12 open as before).

## 2. JSON contract (8698dfb): BLOCKER, two write commands changed behaviour

`emit` now validates through a model. Two write paths store a float from `clock.now()` but the new models type it `str`:

- `models.AlarmAcks = RootModel[dict[str, str]]`, while `alarm ack` stores `data[key] = clock.now()` (float).
- `models.Verdict.at: str`, while `review verdict` stores `"at": clock.now()` (float).

Reproduced with the repo's own test harness (tests/test_core_cli.py `rt`, `invoke`):
`sm --format json alarm ack stall:head.ship:abc` writes the ack, then exits 70 with
`ValidationError ... Input should be a valid string [input_value=1800000960.0, input_type=float]`.
Before this commit it printed the document and exited 0. `review verdict --format json` hits the same: the
write-once receipt is stored, then exit 70; a caller that retries gets "already recorded and cannot be changed".
Text mode is unaffected (emit skips validation). The json contract test checks the tree and schemas, not a json run
of these two writes, so CI stays green.
Fix: type them as what is stored (`dict[str, float]`, `at: float`, or a timestamp type the store uses), and add one
json-mode test per core write command (alarm ack, review verdict, focus add/done). focus add/done json works (checked).
Native write commands: `emit_result` only maps `None` to `Nothing()` (dumps `{}` as before); no change.

## 3. Ratchet and promote

- bin/check full mode runs `bin/debt.py --gate`; a number worse than docs/debt.json exits 1 and turns the gate red.
  Lower findings or higher coverage pass; `--record` writes new numbers (visible in the docs/debt.json diff).
- bin/promote step 1 runs `bin/check` (full, not `--fast`), so the ratchet is in promote. bin/merge-slice runs
  `--fast` (no ratchet, no coverage), as documented.
- Gaps (not blockers, follow-ups):
  a. debt.py skips a metric whose value is None (coverage report failed, no .coverage) and counts 0 when mypy or ruff
     prints no "Found N error" line (crash, changed output). Either passes silently. Fail on None and on a missing
     summary line instead.
  b. A key missing from debt.json is never compared.
  c. `bin/promote --red-gate-ok REASON` passes a red ratchet; explicit, but the reason is only printed, not recorded.
- docs/debt.json says "environment reads plugin": 77 (the brief said 79): lower, allowed.

## 4. Gate run

`sm gate run -- bin/check` (tree = 6acd0b4 + the edits that became f39eccd + the four review files): every gate
PASS (tests, core coverage 93, help lint both projects, typos, ruff, mypy strict core, pyright core, vulture, debt
ratchet) except `coverage plugin >= 90`: 88. PROMOTE GATE RED on plugin coverage only, as expected (pending).

## 5. Not verified

- bin/check ran on the working tree described in section 4, not on a clean checkout of one SHA.
- Live promote not run. Plugin coverage < 90% stays red (pending, expected).
- The skill hz-build names `sm mini gate`; that command does not exist (`sm gate status` is the one). Doc gap.

VERDICT: REQUEST_CHANGES (section 2: `alarm ack` and `review verdict` exit 70 in json mode after writing)
