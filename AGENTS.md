# Agent instructions for this repository

You work on `steuermaschine` (`sm`), a Typer CLI that steers changes from commit to live release. Read
`docs/INTEGRATION.md` before your first change; it wins over your branch at merge. Binding goal:
`docs/DEFINITION-OF-DONE.md`. Where a rule here blocks you, say so in your hand-over note; do not work around it.

## Layout

```
src/steuermaschine/      core: generic, names no project, person, company or private path
plugins/heinzel/         the large plugin: src/steuermaschine_heinzel/{plugin.py,toolmap.py,traps.py,native/,reviews/,parity/}, tests/
examples/toy, layer/     small plugins; the toy is the quick start, the layer proves plugin stacking
tests/                   core tests (tests/test_core_<module>.py), tests/projects, tests/gate_known.json
docs/                    DESIGN, INTEGRATION (the contract), DEFINITION-OF-DONE, GAPS, NATIVE-PORT
bin/                     check, promote, rollback, merge-slice, sm-dev, debt.py, gate_known.py
```

Native commands live in `native/<area>.py` (plus `native/<area>_lib/`), tests in
`plugins/heinzel/tests/test_native_<area>*.py` with unique basenames repo-wide.

## Hard rules (condensed from docs/INTEGRATION.md)

- **Review units and digests.** A write command (`writes=True`) is off until `reviews/<path>.md` ends with
  `VERDICT: APPROVE` for the exact digest of its unit (module plus its `*_lib`). `sm self reviews` shows unit,
  digest, state. Never write the `REVIEWED:` or `VERDICT:` lines yourself.
  The digest also covers every first-party file the unit imports, transitively (core and other `_lib` packages):
  a change to core voids the verdicts of the units that import it. Every writer is under the gate: core's own
  writers (`review verdict`, `focus add|done`, `alarm ack`, `ci cancel`), engine pipelines (`Pipeline(unit=...)`)
  and every native command whose Changes line names a write (`sm self lint` flags a `writes=` that disagrees). The
  only exception is `exempt="reason"`, honoured only for a path on the core's allowlist `native.EXEMPT_PATHS`
  (`gate run`, the run lock every review's tests run through); any other `exempt=` is still gated.
- **Reach the registry by name.** No module below `native/` or `pipelines/` imports `plugin.py` or `native/__init__.py`,
  and a pipeline imports no command module: one such import puts the whole plugin into the unit's digest (23 commands had
  202 to 225 files that way). Use `pipelines.common.command(path)` or `runtime().commands()`.
  `plugins/heinzel/tests/test_native_digest_locality.py` fails on a wider digest; `steuermaschine.reviewgraph` shows what
  a digest reaches and which verdicts a change voids.
- **Pipelines compose, they do not reimplement.** `sm head admit` and `sm release cut` (plugin `pipelines/`) call
  native commands through `native.call`, which applies that command's own gate; tests replace a step through
  `pipelines.common.OVERRIDE`, never the gate.
- **Never edit an approved unit without a new review.** Any change voids the verdict. A conflict in one goes back to
  its owner.
- **No wrappers.** No command starts an old script, and no `tools_dir` setting exists. A command exists only when it
  is native; what is not ported shows as MISSING in `sm tools gaps`. Do not bring deleted files back.
- **No real-path setting defaults.** No default may point at a real file or directory of a machine (no `~/...`, no
  absolute path); a per-machine value defaults to `None` and the command that needs it refuses and names the key.
  Every setting has a `description`; secrets are `SecretStr`.
- **Tests are sandboxed.** The root `conftest.py` runs tests with HOME, XDG and state dirs in a temp directory and no
  `SM_*`/`HZ_*`/`STEUERMASCHINE_*` variables. No network, no real `gh`, no sleep: use `fake_providers()`,
  `FakeCommands`, `invoke(...)`. A test that writes a real file of the machine fails.
- **Narrow interfaces.** Processes only through `providers.commands`, GitHub through `providers.forge`/`.ci` or
  `steuermaschine.github.Gh`, time through `providers.clock`, state through `providers.store`, digests through
  `steuermaschine.hashing.canonical_sha256`. No `subprocess`, `time.sleep`, `os.environ` or `fcntl` in native code.
- **Never `git stash`.** The stash list is shared by all worktrees of the repository. Commit a WIP instead.
- **One worktree per slice.** `git worktree add ../steuermaschine-wt-<slice> -b <branch> origin/main`; do not work in
  the main checkout, which belongs to the integrator.
- **Heavy runs go through the gate:** `sm gate run -- <command>` (full test suite, type checks, builds, pushes).
  Focused tests may run directly.
- **The core is clean and stays so:** ruff, mypy strict, pyright, 90% line+branch coverage, no dead code (vulture);
  `bin/check` gates all of it. The plugin's typing, ruff and environment-read debt may only fall: `docs/debt.json`
  is the ratchet (`uv run python bin/debt.py --record` in the commit that improves a number).
- **Moved code stays verbatim.** Moved libraries and their moved tests keep their text so the diff against the
  original is the review instrument; they get per-file ruff and mypy exceptions in `pyproject.toml`, and a verifier
  (for example `plugins/heinzel/tests/packet_verbatim.py`) prints every changed line. Moved libraries expose functions only, no
  `if __name__ == "__main__"`.
- **Help and output.** Help in contract form via `H(...)`, every argument and option with its own `help=`;
  `sm self lint` (strict) and `tests/test_core_help_order.py` must stay clean. Every command is `@native` and
  returns a typed `Model` (core commands: `emit(..., Model)`); text through `render()`, JSON from the model
  (`tests/test_core_json_contract.py` runs every command in json and parses it with its model). Errors
  are `Refuse`/`Wait`, never `sys.exit`.
- **Settings.** Typed only: a plugin field with a `description`, or `config.Environment` for a process variable.
  Only `config.py` reads `os.environ` in the core; native modules read none (`tests/test_core_settings.py`).
- **Privacy.** Never print, log or commit a configuration value that may be private (URLs, repository names, tokens,
  paths). No old tool names (`hz-...`) in native output, help or state paths.

## Run the tests and the gate

```sh
uv sync
uv run pytest plugins/heinzel/tests/test_native_<area>.py     # focused
sm gate run -- uv run pytest                                  # everything
bin/check --fast      # tests, help lint per project, typos, ruff, mypy strict and pyright (core), vulture
bin/check             # the same plus coverage >= 90 (core, plugin) and the plugin's debt ratchet (docs/debt.json)
```

## How a change reaches the live `sm`

1. Build on a branch in your own worktree, small commits, tests green, rebased on `origin/main`.
2. Hand over (branch, commit range, test count, files outside your area, settings added, what you could not verify;
   see INTEGRATION.md section 7). The integrator merges with `bin/merge-slice BRANCH`, which refuses on red.
3. Promotion: `bin/check`, then `bin/promote --check`, then `bin/promote`. It builds the committed HEAD into the
   stable install and switches atomically; the coordinator decides when, never during a running admission or release.
   It refuses while a gated write command is not approved at its digest: a change that moves a digest is not live
   until that unit's new verdict is merged (`sm self reviews`).
4. `bin/rollback` switches back to the previous release (run it again to go forward).

## Commits

Small, one area or command per commit; subject at most 72 characters. End every commit message with the trailer of
the model that wrote it, for example:

```
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```

## Never

- Push to `main`, or merge into it, without the integrator.
- Edit the stable install (`~/Code/steuermaschine-stable` or whatever `SM_STABLE` names); it changes only through
  `bin/promote` and `bin/rollback`.
- Print configuration values, or put a private value in help, a test or a commit.
- Edit an approved review unit, a `reviews/` verdict, or `tests/gate_known.json` by hand.
- Run an old script, or add a wrapper around one.
- Change repository settings, or open or merge a PR unless asked.
