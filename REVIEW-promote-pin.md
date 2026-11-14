# Review: fix/promote-pin at 49bc13c (promote builds the checked commit; launcher pins one release per tree)

Independent reviewer; I wrote none of this change. One commit on c7c6474; it touches only `bin/promote`.

## What holds (file:line at 49bc13c)
- Checked commit = built commit. `bin/promote:25` records the full HEAD before the gate; `:39` refuses when HEAD
  moved or the tree became dirty while `bin/check` ran; `:43` archives `$full`, not `HEAD`. A reused release dir
  (`:41`) is keyed by the same 12-char id, so it holds the same commit. `:120` re-checks HEAD before the switch.
  The switch (`:138-150`) names `releases/$id` only. No path switches to an unchecked commit.
- Real run in a temp clone (`SM_STABLE` and `HOME` pointed at a scratch dir, `bin/check` replaced by a stub):
  a stub that commits during the gate -> `REFUSED: the checkout moved while the gate ran`, rc 1, nothing built;
  a stub that edits a tracked file -> same refusal. The live stable was never touched.
- Pinning. The launcher heredoc (`:124-136`) extracted into a temp stable with two fake releases: a parent `sm`
  that flips `current` to the other release and then starts a child `sm` -> the child runs the parent's release
  code and config (`STEUERMASCHINE_PINNED`, `:130-133`); a fresh `sm` after the flip runs the new release. A pin to a
  missing directory falls back to `current`.
- Loops still end on a switch: `STEUERMASCHINE_RELEASE` stays the `current` pointer (`:134`), and
  `src/steuermaschine/native.py:246-251` compares its realpath per tick.
- Rollback: `SM_STABLE=<temp> bin/rollback` swapped current/previous atomically and the launcher then ran the
  rolled-back release (fresh tree). `bin/rollback` does not write the launcher, so this change does not affect it.
- Atomic launcher: written to `$stable/bin/.sm.$$` and moved with `mv -f` onto `$stable/bin/sm` in the same
  directory (`:124`, `:137`): rename(2), so a reader sees the old or the new file, never a truncated one.
- Review units: merging this branch into main (f4ec420) leaves `sm self reviews` unchanged (only the three
  daemon units loadguard, governor and leakwatch are open, as on main). Nothing went stale.

## Blocking
1. The pin is not confined to the stable releases. `:130` accepts any directory that has an executable
   `venv/bin/steuermaschine`. Shown for real: `STEUERMASCHINE_PINNED=<scratch>/evil <stable>/bin/sm x` ran
   `evil/venv/bin/steuermaschine`. So any user or agent can make the stable `sm` (the one the hooks and
   shims call) run unreviewed, unpromoted code by setting one variable, which bypasses the review gate, and a
   pin inherited from another stable root (a test layout) is honoured too. Fix: accept the pin only when its
   physical path is a direct child of `$root/releases`, e.g.
   `p="$(cd "$STEUERMASCHINE_PINNED" 2>/dev/null && pwd -P)"; r="$(cd "$root/releases" && pwd -P)"; case "$p" in "$r"/*/*|"") ;; "$r"/*) [ -x "$p/venv/bin/steuermaschine" ] && rel="$p" ;; esac`
   (else resolve `current`), plus a test that runs the generated launcher with a foreign pin and with a valid pin.

2. The branch is red. `bin/check --fast` fails
   `plugins/heinzel/tests/test_native_guards_smprocs.py::test_the_real_processes_are_the_venv_python_running_the_entry_not_the_word_sm`.
   The test builds its stable layout from the launcher text in `bin/promote` (test `:38-75`) and asserts at `:134`
   that the launcher execs `.../stable/current/venv/bin/steuermaschine`. The new launcher execs the resolved
   `.../stable/releases/<id>/venv/bin/steuermaschine` (`bin/promote:131,135`), as intended. The process guard
   itself already accepts `<root>/releases/<id>/venv` (`guards_lib/smprocs.py:13,108`), so only the test
   assertion has to follow the new exec path. Update `:134` (and anything else in that test that keys on
   `current/venv`), then rerun the test through the gate.

## Not blocking (follow-ups)
- `native.py:246` takes the loop's baseline release after the first tick. A switch during the first tick makes
  a loop keep running old pinned code until the next switch. With the pin available, the baseline should be
  `STEUERMASCHINE_PINNED` (the release this process actually runs). This race was already there before the change.
- `bin/promote:151` runs `ln -sfn "$stable/bin/sm" "$HOME/.local/bin/sm"` even with `SM_STABLE` overridden, so a
  test promote against a temp root repoints the live `sm`. A test run must also set `HOME`. This was already there before the change.
- No automated test covers the launcher or the HEAD checks. A small shell test with a stub `bin/check`, like
  the one above, would keep them from regressing.

## Commands
- Temp promote runs and launcher simulation under the session scratch directory (stable root, HOME, clone).
- `sm gate run -- bin/check --fast`: check: FAIL tests (the smprocs test above); help lint, typos, ruff PASS;
  PROMOTE GATE RED: tests.

VERDICT: REQUEST CHANGES (1: STEUERMASCHINE_PINNED must only name a release under $root/releases; 2: smprocs test red)
