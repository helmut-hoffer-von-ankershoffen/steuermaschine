# steuermaschine

**The artificial helmsman.** `sm` is a coordinator CLI that steers a change from commit to live release for projects
in which AI agents do the work. It keeps resumable state per head and per release, always knows the one next command,
and stops at review points where only the owner or an independent reviewer may continue.

[Source](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine) ·
[Changelog](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/blob/main/CHANGELOG.md) ·
[Issues](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine/issues)

## Install

Supported on **macOS and Linux** with Python 3.15 and newer. With [uv](https://docs.astral.sh/uv/):

```sh
uv tool install steuermaschine   # puts `sm` and `steuermaschine` on your PATH
```

## What it does

Bare `sm` (same as `sm go`) is read-only and prints, in this order:

- `PHASE`: where the project stands.
- `NEXT`: the one command to type now.
- `TRAP`: the mistake that is most likely at exactly this step.

`WAITING` appears when the run waits on the outside world (CI, a reviewer); run the same command again later.
Exit codes: 0 done or a planned stop, 1 refused, 2 usage, 3 alarm, 70 tool error or plugin not loaded, 75 waiting.
Every command has `--format json` with exactly one document and a published schema (`sm self schema`).

The core knows no project: pipelines, providers, gates and traps come from plugins, wired with
[pluggy](https://pluggy.readthedocs.io/). Write commands stay closed until an independent review approves the exact
digest of their code.

## Status

Alpha. The plugin API (`steuermaschine.API_VERSION`) may still change. A toy plugin that needs no account, and the
quick start that drives it, are in the [repository](https://github.com/helmut-hoffer-von-ankershoffen/steuermaschine#try-it).
