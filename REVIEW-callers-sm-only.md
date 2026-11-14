# Review: chore/callers-sm-only at 41578b3 (ffdd414 render, 41578b3 tools gaps), on approved 8ed8051

Independent reviewer; I wrote none of this change.

## ffdd414: no empty line when a result renders nothing
- `src/steuermaschine/app.py:270-273`: the JSON branch is untouched, so `--format json` still emits exactly one
  `json.dumps` document (list, object or `{}`). In text mode a model whose `render()` returns `[]` now prints nothing
  instead of `"\n"`. A render of `[""]` still prints one newline, and every non-empty render is unchanged, so no real
  text is lost. `sm guard uv` is now byte-exact (`test_native_guards_uv.py:61`).
- **Regression in the suite:** `plugins/heinzel/tests/test_native_coord_tasks.py:170` asserts
  `sm beads tasks ls` prints `"\n"` when `bd list` returns non-JSON. It fails now (`'' != '\n'`). The commit did
  not update it. The new behaviour is fine (an empty list printed one blank line before), but the branch is red
  and the test must change to `== ""`. Better still, that path should say why it is empty (bd output unreadable).
  That is a follow-up and was already like this before the change.
  `test_native_guards_lim.py:57` (`lim -- true` -> `"\n"`) still passes: lim prints on its own.

## 41578b3: group rows native only with every old subcommand
- `src/steuermaschine/toolmap.py:20-21`: a row with `requires` is native only when every listed path is in
  `paths` (plugin-ported). The generic "any child makes a group native" rule is gone from `app.py:225`. A group row
  without `requires` now resolves by `row.path in paths` (the test's `old-bare` -> missing).
- Checked against the real tree (`sm self tree --format json` vs `sm tools map --format json` on this branch):
  every map row whose path is a group has `requires`. These are exactly ci one, ci quarantine, head packet and
  release rehearse. Every `requires` path exists, and each list matches the old tool's subcommands:
  - hz-ci-one: rerun, dispatch, status, repeats, watch (old argparse `hz-ci-one:653-682`) = 5 required.
  - hz-rehearse: plan, start, tick(-> `daemon rehearse`), step, cancel, consumers, status = 7 required.
  - hz-admit-wave: preopen, predict, bind, wave = 4 required.
  - hz-quarantine: list (no-arg mode). The old `--digest/--line` mode became `ci quarantine flag`, which is not
    in `requires`. It exists, so the status is right, but add it so a later removal shows as missing (nit).
- Guard rows: hz-guard -> `guard git`, hz-testguard -> `guard heavy`, hz-uv-guard -> `guard uv`. All three exist and
  resolve native.
- `go_lib/words.py:34` now says `sm ci sonar-close` (exists); `test_native_go.py` updated.
- Result: `sm tools gaps` = 3 missing (head admit, ci flakes, mini loops: no such commands, correctly missing),
  66 native, 5 internal, 10 retired. No tool is wrongly native or wrongly missing.
- Already like this on main, not from this branch: some `also` entries name commands that do not exist
  (`alarm clear`, `alarm raise`, `release cut`, `release verify --mini`, `beads triage`). They do not affect status.
  They do mislead `tools where`, so this is a follow-up.

## Review units
Merging this branch into main (f4ec420) leaves `sm self reviews` unchanged. The only open units are daemon
loadguard, governor and leakwatch, and they are open on main too. Nothing went stale. On the branch itself the
rehearse units show open only because it predates 88d9cde; the merge clears that.

## Commands
- `sm gate run -- uv run pytest tests/test_core_native.py plugins/heinzel/tests/test_native_guards_uv.py
  plugins/heinzel/tests/test_native_go.py plugins/heinzel/tests/test_native_coord_tasks.py
  plugins/heinzel/tests/test_native_guards_lim.py`: 1 failed (test_due_ls_and_show_only_read), rest pass.
- `sm gate run -- bin/check --fast`: check: FAIL tests (the same test_due_ls_and_show_only_read); help lint, typos, ruff PASS; PROMOTE GATE RED: tests

VERDICT: REQUEST CHANGES (update test_native_coord_tasks.py:170 to the new empty output; then approve)
