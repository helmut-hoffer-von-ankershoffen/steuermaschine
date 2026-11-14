# Integration contract for the slices

For every agent that hands a branch to the integrator (`main` in `~/Code/steuermaschine`). Where this page and your
branch disagree, this page wins at merge. If something here blocks you, say so in your hand-over note; do not work
around it.

Binding goal: [DEFINITION-OF-DONE.md](DEFINITION-OF-DONE.md).

## 0. Ground rules

- Rebase on `main` before you hand over. `main` moved to pluggy after your branch point (`11fd782`): there is no
  `Plugin` base class any more. A plugin is an object with `@hookimpl` functions (`steuermaschine.hookspecs`).
  `sm self hooks` lists the specifications.
- **No wrappers.** No command may start a script of the old tools directory. On `main` the plugin's `app.py`,
  `ops.py`, `admit.py`, `run.py`, `translate.py`, `adapter.py`, `providers.py`, `steps.py`, `settings.py` and their
  tests are deleted and the setting `tools_dir` is gone. If your branch edits one of them you get a modify/delete
  conflict: move the logic you need into `native/<area>.py` or `native/<area>_lib/`; do not bring the file back.
  A command exists only when it is native. What is not ported is MISSING in `sm tools gaps`.
- Commit small and hand over early: one area, or one command, at a time.
- One commit series per slice, tests green with `uv run pytest`, no edits outside your area (section 1) except the
  one-line registrations named in sections 3 and 5.
- Nothing in core may name Heinzelmännchen, a repository, a company, a person or a private path.
- Native code never prints, logs or stores an old tool name (`hz-...`) or the old command name. State paths
  included.
- A write command is off until its review file is approved (section 3). You do not write the verdict line.

## 1. Layout and ownership

```
src/steuermaschine/                 CORE (generic; another project uses it without our plugin)
  hookspecs.py plugins.py           hook specifications, plugin stack            integrator
  engine.py go.py                   state machine, go contract                   integrator
  native.py                         the native registry (section 3)              integrator
  toolmap.py hashing.py             tool map mechanics (`sm tools map|where|gaps`), canonical sha256      integrator
  models.py render.py               result-model base class, text renderer       integrator
  config.py                         settings                                     integrator
  protocols.py defaults.py          provider Protocols, generic defaults, the ONE process runner   integrator
  github/                           Forge, CI, Sonar on `gh`; `Gh` client; `sm ci`, `sm pr`        slice core-github-providers
  runlock.py window.py preflight.py alarms.py   generic mechanisms               slice heinzel-native-ops
  testing/                          fakes, conformance kit                       integrator (add fakes by PR note)

plugins/heinzel/src/steuermaschine_heinzel/
  plugin.py                         hook implementations, settings model         integrator (you add one line, section 5)
  toolmap.py                        data: every old tool, its command path and owner   integrator (fix a row's path if your command is named differently)
  traps.py                          trap texts per pipeline step                 integrator
  native/__init__.py                you add: one import and one entry in MODULES
  native/<area>.py                  native commands of one area                  the slice that owns the area
  native/<area>_lib/                larger moved logic of that area (optional package)
  reviews/<command-path>.md         port review + verdict (section 3)            the slice, verdict by an independent reviewer
  parity/<command-path>.md          parity record against the old tool           the slice

plugins/heinzel/tests/
  test_native_<area>*.py            unique basename repo-wide (pytest imports by basename)
  fixtures/<area>/                  recorded outputs, sample state files
```

Areas and owners:

| Area (`native/<area>.py`) | Commands | Slice |
|---|---|---|
| `go`, `alarm`, `status`, `tools`, `mini` (verify), `ci_read` (why, faillog), `queue_read` (list, why), `head_read` (blockers, hash, size), `release_read` (candidate) | read-only | heinzel-native-readonly |
| `admission` | head prepare / apply / packet / admit, release admit (release-main route) | heinzel-native-admission |
| `queue`, `release`, `train`, `daemon` | queue add/drop/run, release resume/cut/drop, train, `sm daemon ...` | heinzel-native-queue-release |
| `dispatch`, `window`, `preflight`, `lock` (gate, lim), `rehearse`, `tick`, `guards`, rest | hosted dispatch, validation window, preflight, run lock, rehearsal, tick/watch, guards | heinzel-native-ops |

If two slices need the same helper: the one named first in this table writes it, in core if it is generic; the other
imports it. Do not copy.

## 2. The narrow interfaces (do not define your own)

| Need | Use | Never |
|---|---|---|
| Start a process | `runtime().providers.commands.run(argv, cwd=None, timeout=None, env=None) -> Result(rc, out, err)`; `.passthrough(argv, cwd=None, env=None) -> int` for a command that owns the terminal | `subprocess` in native code; a second runner class |
| GitHub | `runtime().providers.forge` / `.ci` (Protocols in `protocols.py`), or for calls the Protocols lack: `steuermaschine.github.Gh(commands).json(args) -> Any` / `.run(args) -> Result` | `gh` argv built in your module |
| Time, sleep | `providers.clock.now()`, `.sleep(s)` | `time.time`, `time.sleep` |
| State | `providers.store.load/save/keys(kind, key)`; kind is `"<plugin>.<area>.<thing>"` | files under `~/.cache`, module-level paths |
| Locks | `providers.store.lock(name)` (raises `Busy`); the cross-process run lock: `steuermaschine.runlock` | `fcntl` in your module |
| Receipts | `providers.store.put_receipt(kind, key, data)` (write once) | overwriting |
| Settings | `runtime().settings["field"]` | `os.environ` (the one exception: passing the environment through to a child) |
| Owner notification | `providers.notifier.notify(level, key, text)` | calling a notifier script |
| Canonical digest | `steuermaschine.hashing.canonical_sha256(text)` | a local copy |
| Old state files that must be adopted | read-only, in `native/<area>_lib/legacy.py`, one function per file, named in your hand-over note | writing to an old path |

`Result`, `Busy`, the Protocols and the fakes (`steuermaschine.testing`: `fake_providers`, `FakeCommands`,
`FakeClock`, `MemoryStore`, `invoke`) exist on `main`, as do `Commands.run(..., env=)`, `.passthrough`,
`steuermaschine.github` (`Gh`, `GhError`, `GitHub`, `steuermaschine.github.testing.FakeGh`) and `steuermaschine.native`.

## 3. The native registry (`steuermaschine.native`)

A native command is a function with typed parameters (Typer style) that returns a result model. It is registered
with a decorator; the plugin exposes its list through the hook `sm_native`.

```python
from steuermaschine.helpdoc import H
from steuermaschine.models import Model
from steuermaschine.native import native, runtime

class QueueList(Model):
    entries: list[QueueEntryRow]
    freeze: bool

    def render(self) -> list[str]:            # the text form; json is model_dump()
        return [f"{e.pr:<12} {e.state:<11} {e.head[:8]}" for e in self.entries] + [f"merge freeze: {'on' if self.freeze else 'off'}"]

@native("queue list", writes=False, replaces=("queue list",), help=H(
    "The merge queue: every entry and its state.", "Use when a PR should have merged.",
    "nothing (read-only).", "sm queue list", "sm queue why PR"))
def queue_list(all_: bool = typer.Option(False, "--all", help="Include merged entries.")) -> QueueList:
    rt = runtime()                            # .providers, .settings, .project
    ...
    return QueueList(entries=rows, freeze=frozen)
```

Fields of a registration:

| Field | Meaning |
|---|---|
| `path` | the command path without the program name: `"queue list"`, `"head apply"` |
| `writes` | `False`: reads only (this machine and outside). `True`: writes state other commands depend on, or anything outside this machine |
| `help` | help in contract form, built with `H(...)`; `sm self lint` checks it |
| `replaces` | the wrapped command paths this one takes over (normally the same path) |
| `review` | for `writes=True`: name of the file in `reviews/` (default `<path with dashes>.md`) |
| return type | a `Model` subclass, a list of one, or `None`. It is the schema of `--format json` |

What the core does with it:

- Builds the Typer command at `path` from the function's signature. The return value is rendered: text =
  `model.render()`, json = one document. Exit code = `model.exit_code` (a property, default 0).
- Failure: raise `steuermaschine.engine.Refuse(message, next_cmd, code=1)` or `Wait(message, waiting_on)` (exit 75).
  The core prints the line (text) or the `Error` object (json). Do not call `sys.exit`, do not print errors yourself.
- Progress lines: `runtime().say(text)` (stderr). stdout belongs to the result.
- **When a command is on:**
  - `writes=False`: as soon as it is merged.
  - `writes=True`: as soon as `steuermaschine_heinzel/reviews/<path-with-dashes>.md` ends with a line starting
    `VERDICT: APPROVE`, written by an independent reviewer (the coordinator arranges it; tell the integrator the
    moment your command is review-ready, with the file path). Until then the command exists and refuses with exit 70,
    naming the file. There is no settings switch. In tests: `Project(..., allow_unreviewed=True)`.
- **The tool map** (`toolmap.py`) names for every old tool the command path it becomes and its owner. A row turns
  from MISSING to native by itself when a native command with that path exists. If your command has another path,
  change the row. `sm tools gaps` is the burn-down; its number must only go down on your branch.
- Registration is two lines in `native/__init__.py`: `from . import queue` and `queue` in `MODULES`. A group's help:
  `QUEUE = Group("queue", H(...))` in your module.

Long-running loops (`sm daemon queue`, `sm daemon train`): a native command with `tick=True` (and `writes=True`) that
performs ONE tick and returns a model; the core adds `--loop SECONDS` to it, so every daemon repeats and stops the
same way (json: one document per tick). Do not write your own loop.

Resumable multi-step jobs (`head admit`, `release cut`): engine pipelines (`engine.Pipeline`, hook `sm_pipelines`),
not native functions. Steps call the library code of your native functions; an external step is `external=True`.
The step list of the deleted `steps.py` (in git history at `11fd782`) is the starting point; the new file is
`native/<area>_pipeline.py`, and `plugin.py` returns it from `sm_pipelines`.

## 4. Result models and rendering

- Base class `steuermaschine.models.Model` (pydantic, `extra="forbid"`). Field names snake_case, no abbreviations,
  no tool names. Times are ISO 8601 strings with offset; durations are numbers with the unit in the name
  (`waited_minutes`).
- `render(self) -> list[str]`: the text output, at most 100 characters per line where possible. No ANSI colour.
- `exit_code` property when the command's exit code carries meaning (alarm 3, waiting 75, refused 1).
- Register every model for `sm self schema`: it is picked up from the return annotation of the native function;
  nested models come with it.
- There is no raw envelope: a command without a typed model does not exist.

## 5. Settings

- One typed field per value in the plugin's table `[heinzel]`, snake_case, prefixed by area:
  `queue_batch_minutes`, `admission_receipts_dir`, `dispatch_runner_cap`. Environment: `SM_HEINZEL__QUEUE_BATCH_MINUTES`.
- Define your fields in a mixin in your area module and add it to the bases of `HeinzelSettings` in `plugin.py`
  (the one line you touch there):

  ```python
  class QueueSettings(BaseModel):
      queue_batch_minutes: float = Field(5.0, description="Minutes between two merge batches.")
  ```

- Every field has a `description`. Paths are `Path`. A secret is `SecretStr`.
- No default may contain a person's name or a home directory other than through `~`.
- Environment variables that child processes of the wrapped era read (`HZ_*`) pass through untouched and are not
  settings. A native command does not read them.

## 6. Tests

- `plugins/heinzel/tests/test_native_<area>*.py`; core tests `tests/test_core_<module>.py`. Unique basenames.
- No network, no real tools directory, no real `gh`, no sleep: `fake_providers()` and `FakeCommands.on(prefix, result)`.
- Recorded real outputs go to `plugins/heinzel/tests/fixtures/<area>/` with one line at the top of the test saying
  when and with which command it was recorded.
- Tests that moved with the code keep their assertions; rename only imports.
- Every native command: one test through `invoke(runtime, [...path])` in text and one in `--format json` that
  validates the document against the model.
- Parity: `parity/<command-path>.md` says for which inputs old and new output were compared, and what differs on
  purpose. For a write command also: which side effects, in which order.
- Coverage of your area: at least 90% lines and branches (`bin/check` measures it; it exists on `main` shortly).

## 7. Hand-over note (in your final message, not a file)

1. Branch name, commit range, test count.
2. Commands now native (path, read or write), and the wrapped count before and after.
3. Files you touched outside your area, each with the reason.
4. Settings fields added.
5. Old state files adopted read-only.
6. For write commands: review file, parity record, what the independent reviewer still has to do.
7. What you could not verify.

## 8. Rules added by the reviews (binding for every slice)

### 8.1 What `writes=True` means

A command is `writes=True` when it can do any of this, on any code path, with any option:

- change anything outside this machine: GitHub (a PR, a variable, a run: cancel, rerun, dispatch), a package
  registry, a push, a notification or message, any HTTP request that is not a GET;
- send data to an outside service other than the read-only GitHub and Sonar calls of the core providers (a
  classifier, a model API, a webhook);
- change local state that another process acts on: the merge queue's state, alarm records, holds and freezes,
  release receipts, the tracking document.

Writing its own result files (a packet directory, a prepared transaction, a log) does not make a command a write.
A command with one writing option is split: the option becomes its own `writes=True` command.

The gate enforces a part of this by reading the code (`steuermaschine.lint.write_reach`): a `writes=False` command
must not reach a GitHub client call with a method other than GET, a writing `gh` argument list, a `git push`, or a
write method of the provider Protocols. It is a net, not a proof; the reviewer of a read command still checks.

### 8.2 The review unit and the verdict

- Unit of a command = its module `native/<area>.py` plus `native/<area>_lib/`. Another unit is stated in code:
  `@native(..., files=("queue_lib", "other_lib/part.py"))` (files or directories next to the module; the module is
  always included).
- The unit must contain every `*_lib` package of the plugin that the command's module imports (gate rule). A
  change to any file of the unit voids the verdict. Split a library by review unit when commands with different
  review rhythms share it.
- The review file `reviews/<path-with-dashes>.md` (or the name given with `review=`) contains
  `REVIEWED: sha256=<digest>` and ends with exactly `VERDICT: APPROVE`. A file shared by several commands has one
  line `COVERS: <command path>` per command. `sm self reviews` prints unit, digest and state.
- Nobody but the independent reviewer writes the last two lines. The integrator does not edit an approved unit;
  a conflict there goes back to the owner.

### 8.3 Moved libraries

- A moved library exposes functions only. No `if __name__ == "__main__"`: it is an entry no review gate sees.
  The exception is a file that the code itself runs as a script; it is listed in `tests/gate_known.json` with the
  reason it cannot write, it must stay free of package-relative imports, and its slice has a test that runs it as
  a real process (today: `admission_lib/focused_replay.py`, `queue_lib/main_checkpoint_reader.py`).
- Moved code stays unstyled so the verbatim diff remains the review instrument: add per-file ruff ignores and a
  mypy override for the moved modules and their moved tests in `pyproject.toml` (the admission slice's entries are
  the pattern). New code (`native/<area>.py`, anything in the core) is ruff clean and typed from the start.
- No `subprocess`: processes go through `providers.commands`. No path to an old tool next to the code.
- The integrator does not edit a moved library. A required change is sent to the owner.

### 8.4 Settings

- No default may point at a real file or directory of this machine (no `~/...`, no absolute path). A path or URL
  that differs per machine has no default (`None`); the command that needs it refuses and names the key.
  Values come from the user config, which bin/promote migrates (`~/.config/steuermaschine/migrate-values.toml`).
- A private value (a tailnet URL, a private repository name) is never committed, never in help, never in a test.

### 8.5 Tests

- The root `conftest.py` runs every test with HOME, the XDG directories and the state home in a temporary sandbox
  and without `SM_*`, `HZ_*` and `STEUERMASCHINE_*` variables. A test that opens a real file of this machine for
  writing, or renames or replaces onto one, fails. The live tracking document is not fingerprinted (it is edited while
  gates run); the write guards cover it. The real process runner refuses a real push or GitHub write under pytest.
- A test kit never has a default path. Fixtures live in the repository or are built in `tmp_path`.
- A parity test that needs something outside the repository (a local clone with recorded commits) skips without
  it and is not part of the promote gate; vendor the objects as a fixture bundle if the test should gate.

### 8.6 Known structural findings

`tests/gate_known.json` lists, per rule and with the owning slice, what was in a slice before the rule existed.
A finding that is not listed fails the gate; a listed one that is gone fails too (remove the line in the same
commit). It is recorded once, at a slice's first merge (`bin/merge-slice BRANCH --accept-structural OWNER`).

### 8.7 Merging

`bin/merge-slice BRANCH`: merge without commit, union of the two registration files, `bin/check --fast`, commit or
abort. A conflict in any other file is refused, except in the slice's own area, where the slice's version is
taken as delivered.
