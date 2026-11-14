# Independent review: feat/core-layering at 71deef3

Reviewer did not write any of it. Base f9134cc (merge base with origin/main). DoD: research/sm-definition-of-done.md items 5 and 6.

## Scope
`git diff --stat f9134cc..71deef3`: docs/DESIGN.md, examples/layer/tests/test_layer_stack.py (new),
examples/toy/src/steuermaschine_toy/plugin.py (+`sm_native`), examples/toy/src/steuermaschine_toy/reset.py (new).
No file under `plugins/` or `src/` changed.

## Checks
1. Heinzel review units unchanged: `uv run sm --format json self reviews` at f9134cc (fresh detached worktree, removed afterwards)
   and at 71deef3 give byte-identical output (55 rows, same digests and states, no `sim` row).
2. Order rule proven: `tests/test_core_layers.py` (wrappers nest with the later plugin outside; outer veto before inner sees
   the step; keyed results base first, later entry replaces; outermost provider wins; order from the settings list,
   unlisted plugins blocked) and `examples/layer/tests/test_layer.py` (the layer's trap replaces the toy's; without the
   layer in the list no window step). The new stack test adds the three-stack matrix: `--help` adds `sim`/`tools` only
   with the toy, the layer's `sm_go_report` line only with the layer loaded, `self lint` clean in every stack.
3. Write gate: `sm sim reset` exits `INTERNAL` (70) with "independent review is missing", names `reviews/sim-reset.md`,
   prints "nothing was run", stdout empty, the world file untouched, also under `--format json` and with layer
   settings toggled; the core alone exits 2 (no such command). `self reviews` shows the unit `missing`, files
   `["reset.py"]`, 64-char digest, identical with and without the layer.
4. Toy write unreachable with only the heinzel plugin: in this worktree (toy and layer installed in the env, config
   loads `heinzel` only) `uv run sm sim reset` exits 2 "No such command 'sim'"; `sm --help` has no `sim`. `plugins.load`
   blocks every unlisted plugin.
5. DESIGN.md vs code: hook table matches `hookspecs.py`; collecting hooks go through `Stack._collect` (reversed pluggy
   order, base first, keyed dict update so later wins); provider hooks and `sm_raw` are `firstresult` (pluggy calls
   the last registered first = outermost); `sm_step_run` wrapper with core `trylast` innermost; `sm_go_report` is
   collected base first. §13 measures DoD 5 and moves nothing; the blockers (review unit digests) are stated.

## Runs
- `sm gate run -- uv run pytest examples/layer/tests/test_layer_stack.py examples/layer/tests/test_layer.py examples/toy/tests/test_toy.py tests/test_core_layers.py -q`: 26 passed.
- `sm gate run -- bin/check --fast`: PROMOTE GATE GREEN (tests, help lint heinzel and layered, typos, ruff).

## Notes (non-blocking)
- DESIGN.md says a layer that replaces a write command's path brings its own review unit; `Stack.native` keys by path so
  the later plugin's command replaces, but no test shows a layer-replaced write path being gated by the layer's own
  review file. Worth one test in the refactoring pass (DoD 15).
- `test_the_layer_wraps_engine_steps_but_cannot_open_the_review_gate` only toggles the two layer settings; it is a
  weak proof, the real guarantee is `tests/test_core_native.py::test_no_environment_variable_and_no_settings_file_can_switch_the_review_gate_off`.
- DoD 5 stays partial (measured, plan, nothing moved), as the branch says.

VERDICT: APPROVE
