# steuermaschine

[![CI](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/ci.yml/badge.svg)](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/ci.yml)
[![Coverage](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/raw/badges/coverage.svg)](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/ci.yml)
[![CodeQL](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/codeql.yml/badge.svg)](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/codeql.yml)
[![PyPI](https://img.shields.io/pypi/v/steuermaschine)](https://pypi.org/project/steuermaschine/)
[![Python](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml)
<!-- SonarQube Cloud: the project is private, so its badges need a project badge token. Owner: SonarQube Cloud >
     project > Information > Badges, copy the token, and replace BADGE_TOKEN below (all three). -->
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=alert_status&token=BADGE_TOKEN)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)
[![Security](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=security_rating&token=BADGE_TOKEN)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)
[![Reliability](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=reliability_rating&token=BADGE_TOKEN)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)

`sm` is a reusable coordinator that steers a change from commit to live release for projects in which AI agents do
the work. It keeps resumable state per head (a commit on a work branch) and per release, always knows the one next
command, and stops at review points where only the owner or an independent reviewer may continue. The core knows no
project; pipelines, providers, gates and traps come from plugins (core plus plugins, wired with pluggy).

The name: not a Steuermann or Steuerfrau, a human helmsman, but the artificial one: a Maschine that steers. The
owner sets the destination and decides at the review stops.

## Status

Pre-release and private. Version 0.0.1. Every command is native; no command starts an old script
(`sm tools gaps --wrapped` prints 0, `sm tools gaps` the rows not ported yet). The binding goal for the first
release is [docs/DEFINITION-OF-DONE.md](docs/DEFINITION-OF-DONE.md). The plugin API (`steuermaschine.API_VERSION`)
may still change.

## Install

From a clone, for now (PyPI later, project name `steuermaschine`):

```sh
git clone <repository-url> steuermaschine && cd steuermaschine
uv tool install .          # puts `sm` and `steuermaschine` on your PATH
```

For development use `uv sync` and `bin/sm-dev` instead (see Development).

## Quick start (toy plugin, no account needed)

```sh
uv sync && export PATH="$PWD/.venv/bin:$PATH"
mkdir /tmp/toy && cd /tmp/toy && git init -q -b main && git commit -q --allow-empty -m init
printf '[project]\nname = "toy-demo"\nplugin = "toy"\n' > steuermaschine.toml
git checkout -q -b feat/hello && echo hi > hello.txt && git add hello.txt && git commit -q -m hello
sm                         # PHASE idle
sm head ship .             # dry run: stops before the first write outside this machine, prints what it would do
sm head ship . --live      # pushes, opens the PR, then WAITING on CI
sm sim ci green .          # play the outside world: CI turns green
sm head ship . --live      # continues where it stopped: merge, tag, finished
```

Run the `uv sync` line in the clone, the rest in the scratch directory. The toy's forge and CI are a simulation in
`.sim/world.json`; the engine, state, dry run, waiting and resuming are real. State lives under
`~/.local/state/steuermaschine/<project>` (`STEUERMASCHINE_HOME` moves it).

## The `go` contract

Bare `sm` (same as `sm go`) is read-only and prints, in this order:

- `PHASE`: where the project stands.
- `NEXT`: the one command to type now.
- `TRAP`: the mistake that is most likely at exactly this step. Read it before you run NEXT.

`WAITING` appears when the run waits on the outside world (CI, a reviewer); run the same command again later.
Exit codes: 0 done or a planned stop, 1 refused, 2 usage, 3 alarm, 70 tool error or plugin not loaded, 75 waiting.
Every command has `--format json` with exactly one document and a published schema (`sm self schema`).

## The review gate

- A command that writes outside this machine, or changes state other commands act on, is a write command.
- Write commands stay closed (exit 70, naming the review file) until an independent review approves them.
- The approval names the exact digest of the reviewed code unit: the module, its `*_lib` package and every
  first-party file it imports, transitively. Any edit to one of them voids it, also an edit to the core.
- The reviewer is never the author: `sm review verdict DIGEST --approve --reviewer NAME` records it.
- `sm self reviews` lists every write command, its unit, its digest and its state.

- `bin/promote` refuses a build in which a gated write command is not approved: promoting it would switch that
  command off.

Writes that a pipeline step makes also need `--live`; without it a run stops with a dry-run plan. A pipeline
(`sm head admit`, `sm release cut` in the Heinzelmännchen plugin) runs each step as a native command through that
command's own gate, and stops at its review points until a different agent approved the digest.

## Writing a plugin

A plugin is an object (or module) with functions marked `@hookimpl`; the core calls the ones it implements:
`sm_api_version`, `sm_settings`, `sm_pipelines`, `sm_gates`, `sm_traps`, providers such as `sm_forge` and `sm_ci`,
`sm_native` (commands: `@native` functions that return a result model) and more. Every command follows the help
contract (`sm self lint`, strict) and prints one typed JSON document with `--format json`. Register it as an
entry point in group `steuermaschine.plugins` and name it in the project's
`steuermaschine.toml` (`plugin = "..."`, or `plugins = ["base", "outer"]`; later entries are outer layers).
`sm self hooks` lists the specifications, `sm self plugins` the loaded stack. Start from `examples/toy` (a complete
plugin in one file), `examples/layer` (a second plugin layered on the first), `src/steuermaschine/hookspecs.py` and
section 5 of [docs/DESIGN.md](docs/DESIGN.md). Test with `steuermaschine.testing` (`fake_providers`, `invoke`,
conformance kit).

## Development

```sh
uv sync                    # workspace: core, plugins/heinzel, examples/toy, examples/layer
uv run pytest              # all tests, sandboxed (see docs/INTEGRATION.md section 8.5)
bin/sm-dev ...             # run the development tree
bin/check                  # the quality gate: tests, coverage >= 90, help lint, typos, ruff, mypy strict,
                           # pyright, dead code, the plugin's debt ratchet (docs/debt.json); --fast skips coverage
bin/promote --check        # everything a promote would test, without switching
bin/promote                # build the committed HEAD into the stable install and switch to it
bin/rollback               # switch the stable install back to the previous release (again: forward)
```

A stable install differs from the development tree: `sm` on the PATH runs a commit built with `git archive` into
its own directory and virtual environment, non-editable, and nothing in it points at the checkout. Only
`bin/promote` changes it, so editing or breaking the tree never breaks the live tool.

## Docs

- [docs/DESIGN.md](docs/DESIGN.md): purpose, pipelines as state machines, providers, plugins, help contract, output.
- [docs/INTEGRATION.md](docs/INTEGRATION.md): the contract for every change handed to the integrator.
- [docs/DEFINITION-OF-DONE.md](docs/DEFINITION-OF-DONE.md): the binding goal for the first release.
- [docs/NATIVE-PORT.md](docs/NATIVE-PORT.md) and [docs/GAPS.md](docs/GAPS.md): how the old tools were ported.
- [docs/RUNBOOK-release-cut.md](docs/RUNBOOK-release-cut.md): a release with `sm release cut`.
- [docs/SWITCH-queue-daemon.md](docs/SWITCH-queue-daemon.md): switching the live queue daemon.
- [AGENTS.md](AGENTS.md): instructions for coding agents working in this repository.
