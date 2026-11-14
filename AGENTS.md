# Agent instructions for this repository

You work on `steuermaschine` (`sm`), a Typer CLI that steers changes from commit to live release. Where a rule here blocks you, say so in your hand-over; do not work around it.

## One repository

All work happens here, in the public `steuermaschine` repository: branch from `origin/main`, open a pull request,
merge after CI and review. The earlier private history is archived and never comes back: the public history starts at
the commit in `scripts/public-root`, and both the pre-push hook and CI refuse any commit that does not descend from it
(`scripts/check_public_root.py`). Push branches only; tags come from releases.

Everything you commit is public at once. Write nothing that must stay private: no names, emails, home paths
(`~/...` instead), machine names or private repository names. `scripts/check_confidentiality.py` (tree and
`--messages`) and gitleaks run in CI on every push and pull request. Commit with the GitHub noreply identity.

## Layout

```
src/steuermaschine/      core: generic; names no project, person, company or private path
plugins/heinzel/         the large plugin: src/steuermaschine_heinzel/{plugin.py,native/,pipelines/,reviews/}, tests/
examples/toy, layer/     small plugins: the quick start, and proof that plugins stack
tests/                   core tests (tests/test_core_<module>.py), tests/projects
docs/                    DESIGN.md and the runbooks
bin/                     check, promote, rollback, sm-dev
```

Native commands live in `native/<area>.py` with their logic in `native/<area>_lib/`. Their tests are `plugins/heinzel/tests/test_native_<area>*.py`, with basenames unique in the repository.

## Workflow

1. Branch from the current integration branch (ask the coordinator which; `main` may be behind it) in your own worktree: `git worktree add ../sm-<topic> -b <branch> origin/<integration-branch>`.
2. Small commits, focused tests green, rebased on the integration branch.
3. Run `bin/check --fast` while you work, and `bin/check` once before you hand over.
4. Hand over in your final message: branch, commit range, test count, files outside your area, settings added, what you could not verify. The coordinator merges.

```sh
uv sync
uv run pytest plugins/heinzel/tests/test_native_<area>.py     # focused
bin/sm-dev ...                                                # run the development tree
bin/check --fast      # tests, help lint, typos, ruff, mypy strict and pyright (core), dead code
bin/check             # the same plus coverage >= 90 % (core, plugin) and the plugin's debt ratchet
make check            # what hosted CI runs: lint, type check, tests with coverage, secret scan
```

`bin/check` is stricter than `make check`. Green CI alone is not enough for a promote.

## How a change goes live

The `sm` on the PATH is a stable install: a commit built with `git archive` into its own directory and environment. Nothing in it points at a checkout.

- `bin/promote --check` tests everything a promote would, without switching.
- `bin/promote` builds the committed HEAD, runs the smoke list and the review gate, and switches atomically.
- `bin/rollback` switches back.

Only the coordinator promotes, and never during a running admission or release.

## Review

Two different things carry the word.

**Verdicts on a run** are product behaviour. A pipeline stops at a digest of prepared bytes; a reviewer who is not the author records `sm review verdict DIGEST --approve --reviewer NAME`; the apply step refuses without it. Do not weaken this.

**Verdicts on code** decide whether a write command may run at all. Today each write command is closed until its file in `reviews/` names the exact digest of its code and ends with `VERDICT: APPROVE`. The digest covers the command's module, its `_lib` package and every first-party file they import. `sm self reviews` lists the state.

- Never write a `REVIEWED:` or `VERDICT:` line yourself. Only the independent reviewer does.
- A change to a file in a digest voids that verdict. Seven core files are in every digest: `__init__.py`, `config.py`, `engine.py`, `helpdoc.py`, `models.py`, `native.py`, `protocols.py`. Touch them only when the task needs it, and say so in the hand-over.
- Do not delete review files. The installed tool reads them; without them every write command refuses.
- Do not widen a digest: no module below `native/` or `pipelines/` imports `plugin.py` or `native/__init__.py`. Reach other commands by name with `pipelines.common.command(path)` or `runtime().commands()`.

This per-command gate is planned to be replaced by one independent verdict per promoted commit. Until that change is live, the rules above hold.

## Hard rules

- **No wrappers.** A command exists only when it is native. No command starts a script.
- **Pipelines compose.** A pipeline step calls a native command through `native.call`, which applies that command's gate. Tests replace a step through `pipelines.common.OVERRIDE`, never the gate.
- **Narrow interfaces.** Processes through `providers.commands`, GitHub through `providers.forge` and `.ci` or `steuermaschine.github.Gh`, time through `providers.clock`, state through `providers.store`, digests through `steuermaschine.hashing.canonical_sha256`. No `subprocess`, `time.sleep`, `os.environ` or `fcntl` in native code.
- **Typed settings only.** A plugin field with a `description`, or `config.Environment` for a process variable. No default points at a real path of a machine: a per-machine value defaults to `None`, and the command that needs it refuses and names the key. Secrets are `SecretStr`.
- **Help and output.** Help in contract form through `H(...)`; every argument and option has its own `help=`. Every command returns a typed model and prints exactly one JSON document with `--format json`. Errors are `Refuse` or `Wait`, never `sys.exit`. `sm self lint` must stay clean.
- **Write means write.** A command that changes anything outside the machine, or local state another process acts on, is `writes=True`, and its help says what it changes.
- **Tests are sandboxed.** HOME, XDG and state directories are temporary; no network, no real `gh`, no sleep. Use `fake_providers()`, `FakeCommands` and `invoke(...)`.
- **The core stays clean.** ruff, mypy strict, pyright, 90 % line and branch coverage, no dead code. The plugin's debt in `docs/debt.json` may only fall.
- **Privacy.** Never print, log or commit a configuration value, a home path, a person's or company's name, a token or a private repository name. `scripts/check_confidentiality.py` and gitleaks gate the tree and the commit messages.
- **Heavy runs go through the run lock:** `sm gate run -- <command>` for the full suite, type checks, builds and pushes.
- **Never `git stash`.** The stash is shared by all worktrees. Commit a WIP instead.

## Commits

Conventional Commits, one area per commit, subject at most 72 characters. End the message with the trailer of the model that wrote it.

## Never

- Push to `main` (only the integrator does, after CI and review), push a tag, or push anything from the archived private history (the hooks refuse it).
- Edit the stable install; it changes only through `bin/promote` and `bin/rollback`.
- Edit a verdict line or `tests/gate_known.json` by hand.
- Change release commands while a release is in flight.
- Change repository settings, or open or merge a pull request, unless asked.
