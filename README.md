# steuermaschine

[![CI](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/ci.yml/badge.svg)](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/ci.yml)
[![Coverage](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/raw/badges/coverage.svg)](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/ci.yml)
[![CodeQL](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/codeql.yml/badge.svg)](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/actions/workflows/codeql.yml)
[![PyPI](https://img.shields.io/pypi/v/steuermaschine)](https://pypi.org/project/steuermaschine/)
[![Python](https://img.shields.io/badge/python-3.15%2B-blue)](pyproject.toml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=alert_status)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)
[![Maintainability](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=sqale_rating)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)
[![Security](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=security_rating)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)
[![Reliability](https://sonarcloud.io/api/project_badges/measure?project=helmut-hoffer-von-ankershoffen_steuermaschine&metric=reliability_rating)](https://sonarcloud.io/summary/overall?id=helmut-hoffer-von-ankershoffen_steuermaschine)

`sm` steers a change from commit to live release in projects where AI agents do the work. It keeps resumable state for each head (a commit on a work branch) and each release, always names the one next command, and stops where only an independent reviewer or the owner may continue.

The name: not a Steuermann, a human helmsman, but a Maschine that steers. The owner sets the destination.

## Why

An agent that ships a change has to remember a pipeline: which check comes first, which run to wait for, what must never be done twice. It forgets this at every context reset. `sm` holds that knowledge instead:

- **One next command.** Bare `sm` prints where the project stands, the command to type now, and the mistake most likely at this step.
- **Resumable.** State lives per head and per release. The same command continues where it stopped.
- **Dry by default.** A step that writes outside the machine runs only with `--live`.
- **Review stops.** A pipeline halts at a digest until someone other than the author records a verdict.
- **Built for agents.** Every command has help in one fixed form and prints exactly one JSON document with `--format json`.

The core knows no project. Pipelines, providers, gates and traps come from plugins.

## Install

Supported on macOS and Linux with Python 3.15 and newer.

```sh
uv tool install steuermaschine     # puts `sm` and `steuermaschine` on your PATH
```

This installs the core. A project needs a plugin and a `steuermaschine.toml` that names it.

## Try it

The toy plugin simulates a forge and CI in one JSON file, so this needs no account. It lives in this repository:

```sh
git clone https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine && cd steuermaschine
uv sync && export PATH="$PWD/.venv/bin:$PATH"

mkdir /tmp/toy && cd /tmp/toy && git init -q -b main && git commit -q --allow-empty -m init
printf '[project]\nname = "toy-demo"\nplugin = "toy"\n' > steuermaschine.toml
git checkout -q -b feat/hello && echo hi > hello.txt && git add hello.txt && git commit -q -m hello

sm                         # PHASE idle
sm head ship .             # dry run: prints what it would do
sm head ship . --live      # pushes, opens the PR, then waits for CI
sm sim ci green .          # play the outside world: CI turns green
sm head ship . --live      # continues: merge, tag, finished
```

## The core verbs

| Command | What it answers or does |
|---|---|
| `sm` / `sm go` | Where the project stands and the one next command. Read-only |
| `sm head ...` | Take one head through the project's pipeline |
| `sm release ...` | Take one version through the release pipeline |
| `sm review verdict` / `list` | Record or list an independent verdict on an exact digest |
| `sm pr ...`, `sm ci ...` | One pull request, the CI runs, and why something does not merge |
| `sm focus ...` | The heads or versions the owner declared the critical path |
| `sm alarm ...` | Stalls and failures that need someone now |
| `sm gate run -- CMD` | Run a heavy local command under the machine's run lock; `--bg` starts it in the background, `sm gate wait ID` returns its exit code |
| `sm gate status` | Who holds the run lock, who waits for it and why, the load and the background runs |
| `sm lim CMD` | Run a command and print only what matters |
| `sm self ...` | Configuration, plugins, help lint, the command tree, JSON schemas |

Every command explains itself with `--help`: purpose, when to use it, what it changes, an example, what comes next. `sm self tree` prints the whole tree.

`sm go` prints `PHASE`, `NEXT` and `TRAP`, and `WAITING` when the run waits on CI or a reviewer.

Exit codes: 0 done or a planned stop, 1 refused, 2 usage, 3 alarm, 70 tool error or plugin not loaded, 75 waiting (run the same command again).

## Heinzelmännchen

`sm` was built to run the delivery of Heinzelmännchen (`heinzel` on PyPI): admission of heads, a merge queue, release candidates and the checks after a release. That logic is the plugin in `plugins/heinzel`. It is the reference for a large plugin and is not published as a package of its own.

## Write a plugin

A plugin is an object with functions marked `@hookimpl`. The core calls the ones it implements: settings, pipelines, gates, traps, providers for the forge and CI, and native commands that return a typed result model. Register it as an entry point in the group `steuermaschine.plugins` and name it in `steuermaschine.toml`. Several plugins stack as layers.

Start from `examples/toy` (a whole plugin in one file) and `examples/layer` (a second plugin on top of the first). `sm self hooks` lists the hook specifications. `steuermaschine.testing` has fakes and a conformance kit. The plugin API (`steuermaschine.API_VERSION`) may still change before 1.0.

## Links

- [docs/DESIGN.md](docs/DESIGN.md): pipelines as state machines, providers, plugins, the help contract, output.
- [AGENTS.md](AGENTS.md): how to work on this repository.
- [CHANGELOG.md](CHANGELOG.md)
- [LICENSE](LICENSE): MIT.
