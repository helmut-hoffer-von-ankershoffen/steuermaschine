# steuermaschine: design

Status: 2026-11-14, `main`. The parts marked **built** are in this repository; the rest is design. Every
command is native: no command starts an old script.
The command is `sm`; `steuermaschine` is the same program under its full name.

## 1. Purpose

A Steuermaschine is a machine that steers: not a Steuermann or a Steuerfrau, a person at the helm, but an artificial
helmsman (the Greek word for helmsman, kybernetes, gave cybernetics its name). `sm` is that helmsman for software
delivery: it holds the course from a change to a live release. The owner sets the destination and decides at the
review stops; AI coding agents (one coordinator, several builders, independent reviewers) do the work; `sm` knows
where every piece of work stands and what the one next command is.

The bar for every design choice: **a Sonnet-class coordinator ships with this tool alone.** That means no step that
lives only in a prompt, a habit or a person's memory.

It grew out of one day on which no release shipped. The causes (failures found at the most expensive point; every
correction costing a full cycle of ten hand steps; a queue stalled for 277 minutes with nothing watching; state moved
aside by hand) were mostly structural, and 79 single-purpose scripts could not carry them.

### Non-goals

- Not a CI system, a merge queue or a forge. It drives the ones a project has, through providers.
- Not an agent framework. It briefs and (through a provider) starts agents; it does not run their loop.
- Not a place for project rules. The core knows no project, repository, company or person. Everything
  Heinzelmännchen-specific is in the plugin `steuermaschine-heinzel`.
- No daemon in the core. Loops (queue ticks, alarms) are started by the operating system and call `sm` commands.
- It does not decide what ships. Focus and review verdicts come from the owner and from reviewers.

### Where it goes

| Stage | What | State |
|---|---|---|
| One project, one machine | Coordinator and builders type `sm`; the owner reads the same output | built, live for the Heinzelmännchen project |
| Several projects | One `steuermaschine.toml` per repository; state separated per project name | built, tested with two plugins |
| Fleet | Several coordinators and many builders on several machines and repositories; `Store` becomes shared (section 4), locks and receipts with it; lanes cap work per kind | designed: only `Store` changes |
| Agent skills | Generated from the command tree: `sm self tree --format json` has path, purpose, when, changes, example, next and read-only for every command. A skill is that text plus the rules no command can compute | built: core `skillgen.py`; the plugin's `sm doc skills generate`, `check`, `link` |
| MCP server | The same tree as tools: a read-only command is a tool without confirmation, a writing one carries its `Changes:` text; input schema from the options, output schema from `sm self schema` | designed |
| Dashboard | A page that polls `sm --format json go`, `head status`, `alarm list`. No second source of truth: the models of section 7 are the contract | designed |

## 2. Vocabulary

Maritime words only where there was no good plain word. Words agents must recognise stay as they are: **PR, CI,
check, run, merge, tag, release, branch, commit, main**. They are never renamed.

| Word | Meaning | Kind |
|---|---|---|
| **head** | One unit of work: a commit on a work branch, identified by its SHA. A changed commit is a new head | plain (git's word) |
| **pipeline** | The ordered steps a head or a release goes through in this project | plain |
| **step** | One idempotent action of a pipeline, with a check whether it is already done | plain |
| **gate** | A named condition that must hold right before a step acts (CI green on this exact head) | plain |
| **lane** | A class of work with a cap on unfinished heads (for example one head at a time on the release path) | plain |
| **admission** | The decision that a head may enter CI and the queue, and the record that it did | plain |
| **queue** | The merge queue | plain |
| **candidate** | A release that is cut and being proven, not yet published | plain |
| **release** | A version, from the cut to live | plain |
| **verification** | Proof that a version runs, live and healthy, where users are | plain |
| **alarm** | A stall or failure that needs someone now. ALERT exits 3 until acknowledged | plain |
| **focus** | What the owner declared the critical path. `go` ranks it first | plain |
| **receipt** | An immutable record that something happened (a verdict, an admission). Written once | plain |
| **evidence** | What a receipt points to: the bytes reviewed, a log, a run id | plain |
| **trap** | What went wrong at exactly this step before. Printed at the step, not in a manual | plain |
| **review stop** | A point where the run ends until a different agent approved a digest | plain |
| **course** | The current answer of `sm go`: phase, next command, what it waits on | maritime, used in prose only |
| **helm** | Who drives a run (`--as NAME`); the reviewer must be someone else | maritime, prose only |
| **logbook** | The append-only history of steps and receipts of one run | maritime, prose only |

Discipline: the three maritime words appear in documentation and never in a command, option, JSON field or state
file. Commands use the plain column.

Read verbs and write verbs are disjoint. Read: `status`, `list`, `why`, `check`, `tree`, `config`, `schema`, `lint`,
`go`. Write: `add`, `done`, `ack`, `verdict`, and the pipeline verbs a plugin names (`admit`, `ship`, `cut`). A read
verb never writes, with or without flags.

## 3. The pipeline is a state machine (built: `engine.py`)

One run per **subject**: a head (key: its SHA) or a release (key: its version). A plugin defines the pipeline;
the core runs it.

```
Pipeline(subject="head", verb="admit", steps=[Step, Step, ReviewStop, Step*, ...], identify=...)
                                                                          * external: writes outside this machine
```

### Rules of every run

1. **Resumable.** State is saved after every step, atomically, in one JSON file per subject
   (`<state home>/<project>/state/runs.<subject>.<verb>/<key>.json`). The same command continues.
2. **Idempotent.** Before a step acts, its `probe` asks the world whether it is already true (a receipt exists, the
   remote branch is at this head, the PR is open, the PR is in the queue). If so the step is adopted, also when a
   person did it by hand.
3. **Arguments once.** Inputs (`--set NAME=VALUE`) are remembered in the state. The second command is short.
4. **Dry by default.** A step marked `external` runs only with `--live`. Without it the run stops in front of the
   first external step and prints what it would run. `--plan` prints all steps and writes nothing at all.
5. **Bound to what it was done for.** A step names the fingerprint keys it depends on (`head`, `base`). When one
   moves (main moved), that step and all later ones are void and start again, and the run says so. After the first
   external step is done nothing is voided: the write happened.
6. **One writer.** A lock per subject. A second process exits 75.
7. **Lanes.** A new run in a full lane is refused and the occupants are named.
8. **Gates.** A step lists gates; each is checked right before the step acts.
9. **No lost state.** A step that crashes ends the run with exit 70; the steps before it stay done.
10. **Every run ends with the go contract** (below), in text and in JSON.

### Step outcomes

| A step | Run status | Exit | NEXT |
|---|---|---|---|
| returns a detail line | continues | | |
| raises `Wait(message, waiting_on)` | `waiting` | 75 | the same command |
| raises `Refuse(message, next_cmd)` | `refused` | 1 (2 for usage) | the one command that helps |
| is a `ReviewStop` without verdict | `review` | 0 | the same command with `--approved DIGEST` |
| is external and the run is not live | `dry_run` | 0 | the same command with `--live` |
| was the last one | `finished` | 0 | the pipeline's `done_next` (default `sm go`) |

### Review stops

A `ReviewStop` yields the digest of exact bytes and a brief. The engine:

1. recomputes the digest at every run (changed bytes void an earlier verdict),
2. writes the brief through the `AgentRunner` and stops: `NEXT sm head admit WT --approved DIGEST`,
   `WAITING a reviewer who is not <author>; brief: <where>`,
3. accepts `--approved DIGEST` only if DIGEST is the current digest **and** a verdict receipt exists for it
   (`sm review verdict DIGEST --approve --reviewer NAME`), the verdict is `approve`, and the reviewer is not the
   run's author. The digest alone is not a review.

A verdict is a receipt: written once, never changed. A rejected digest stays rejected; new bytes get a new review.
The reviewer's model is recorded (`--model`). Identity is cooperative (names given by agents), not authentication;
see open decision 3.

### The `go` contract (built: `go.py`, model `GoReport`)

`sm go` (also bare `sm`) is read-only by construction: it receives a store that raises on any write.

```
PHASE    where the most urgent thing stands
NEXT     ONE command that can be run now, or "nothing runnable now"
WAITING  who or what the project waits on, when NEXT cannot move it
TRAP     what goes wrong at exactly this point
ALSO     the other things in flight, one line each, with their command
STATE    counts
```

`sm --format json go` carries the same fields (`phase`, `next`, `waiting_on`, `trap`, `also`, `alarms`, `state`,
`read_only`, `schema_version`). Ranking: unacknowledged ALERT, then runs that can move now (focus first), then
review stops, then waits, then what the plugin adds (`Plugin.go_items`). A wait longer than
`alarms.stall_minutes` (default 15) is an ALERT without anyone having to look: that is the 277-minute stall.

Every pipeline command prints the same four lines for its own subject, so a coordinator never needs a second
command to learn what to type next.

### A head and a release, as the Heinzelmännchen plugin defines them

```
head admit:   preflight -> packet -> prepare -> review (STOP) -> apply* -> push* -> pr* -> queue*
release cut:  rehearsal -> pins (STOP) -> cut* -> published -> live
```

and the toy plugin, to show that the engine does not know either:

```
head ship:    push* -> pr* -> ci -> merge* [gate ci-green] -> tag*
```

## 4. Providers (built: `protocols.py`, `defaults.py`, `testing/fakes.py`)

The core touches the world only through these Protocols. Each has the smallest method set the pipelines need.
Methods that write outside the machine are marked; they are only ever called from an external step.

| Protocol | Methods (`!` writes outside) | Generic default in core | Plugin |
|---|---|---|---|
| `Clock` | `now`, `sleep` | system clock | |
| `Store` | `load`, `save`, `keys`, `lock`, `put_receipt`, `get_receipt` | JSON files, atomic rename, `flock` | a shared store for a fleet |
| `Vcs` | `head`, `branch`, `base`, `dirty`, `remote_head`, `push!` | git | |
| `Commands` | `run` | subprocess | (tests replace it) |
| `Notifier` | `notify` | stderr | push to the owner |
| `AgentRunner` | `brief`, `spawn!` | brief files; `spawn` says "start one yourself" | a real agent launcher |
| `AdmissionPolicy` | `blockers`, `prepare`, `apply!`, `receipt` | none needed: every head is admitted | focused admission (heinzel) |
| `Forge` | `pr_for_branch`, `open_pr!`, `checks`, `active_runs`, `merge!`, `tag!`, `get_variable`, `set_variable!` | core `GitHub` (section 12), not wired by `core_providers` yet | toy: simulated |
| `CIProvider` | `status`, `failures` | core `GitHub` (section 12), not wired yet | toy: simulated |
| `Queue` | `add!`, `entry`, `drop!` | **planned**: merge directly when green | heinzel: its merge queue |
| `ReleaseRecipe` | `candidate_ready`, `pins`, `cut!`, `status`, `drop!` | none: releasing is project-specific | plugin only |
| `Verifier` | `verify` | none | plugin only (heinzel: the Mac mini) |
| `Tracker` | `get`, `search`, `comment!`, `close!` | none | plugin only (heinzel: beads) |

`Vcs` and `Commands` are additions to the list in the brief: the engine needs the local checkout and native commands
need a replaceable process runner (tests use `FakeCommands`).

A provider that is `None` means the project has none; a step that needs it refuses with a clear line.
All of them have fakes in `steuermaschine.testing` that record every external call, so a test can assert that a
dry run made none.

## 5. Plugins (built: `plugins.py`, `testing/conformance.py`)

### Discovery and contributions

A plugin is a Python package with an entry point in the group `steuermaschine.plugins`:

```toml
[project.entry-points."steuermaschine.plugins"]
toy = "steuermaschine_toy.plugin:plugin"
```

The entry point is any object (a module or an instance) whose functions are named like the pluggy hook
specifications in `hookspecs.py` and marked `@hookimpl`; `sm self hooks` lists them with their kind and arguments.
An implementation names only the arguments it needs. Loading fails loudly (exit 70, `sm self config` names the
cause) for an unknown hook, a wrong signature or another `sm_api_version`.

| Hook | Contributes |
|---|---|
| `sm_api_version` (required), `sm_settings` | identity; a pydantic model for the plugin's table `[<name>]` (unknown keys are an error) |
| `sm_forge`, `sm_ci`, `sm_vcs`, ... (one per protocol of section 4), `sm_provider_extras` | providers; `base` has the core defaults |
| `sm_pipelines`, `sm_steps`, `sm_gates`, `sm_traps` | pipelines (`sm <subject> <verb>`), steps inserted into another plugin's pipeline, gates, trap texts |
| `sm_step_run` (wrapper) | policy around EVERY step: a veto with a reason, an audit after it |
| `sm_commands`, `sm_native`, `sm_native_paths` | a Typer app merged at the root (outside the `@native` contract: `sm tools gaps --wrapped` counts its commands); native commands (`@native`, read or write); paths where the engine command stays |
| `sm_go_items`, `sm_go_report`, `sm_alarms` | read-only additions to `go` and the alarms |
| `sm_tool_map`, `sm_help_lint_rules` | old tools and their commands; extra help rules |

Merge rule for commands: a name a plugin defines replaces the core's (the plugin owns that word in its project).
`self` is reserved. Where a plugin lists a path in `sm_native_paths`, the engine command stays and the rest of that
group is merged around it.

### Layers: the order rule

One project loads one or more plugins, in the order of its settings:

```toml
[project]
plugins = ["toy", "layer"]      # base first; LATER ENTRIES ARE OUTER LAYERS
```

Only the listed plugins load; every other installed plugin is blocked. The core's own implementations
(`sm_api_version`, `sm_step_run`) and the core's own native commands (`steuermaschine/commands/`: `gate run`,
`gate status`, `lim`, `doc section`, `ci window status`) are the innermost layer. Per kind of hook, "outer" means:

| Kind | Hooks | Rule |
|---|---|---|
| collecting | `sm_pipelines`, `sm_gates`, `sm_traps`, `sm_steps`, `sm_commands`, `sm_native`, `sm_go_items`, `sm_go_report`, `sm_alarms`, `sm_tool_map`, `sm_help_lint_rules`, `sm_provider_extras`, settings tables | every plugin's result is used, base first; where results are keyed (pipeline id, gate name, trap key, command path, settings key, extras key) the later plugin's entry replaces the earlier one's |
| provider | `sm_forge`, `sm_ci`, `sm_store`, ... (`firstresult`) | the outermost plugin that returns one provides it; inner plugins and the core default are not asked |
| wrapper | `sm_step_run` | the outermost plugin's wrapper runs first and last, around every inner wrapper and the step |

The core's own commands take what a project adds through `sm_provider_extras`, under a key the command's module
names, so the outermost plugin that sets a key decides: `gate.POLICY` (the run lock policy:
`policy(settings, commands, clock, allow_full, cwd) -> runlock.Policy`), `doc.DOCUMENT` (the default FILE of
`doc section`: a callable returning a path), `lim.DIRECTORY` (where `lim` keeps a cut command's full output),
`window.WINDOW` (the project's validation window: `window(runtime) -> steuermaschine.window.Window`). Without a plugin
they use the core defaults: lock files and logs below the project's state directory, no refusals, FILE required, the
window's `Rules()` in the namespace `core`. A plugin that needs a different command, not only different values, registers a native command at
the same path; it replaces the core's.

A layer that replaces a write command's path brings its own write unit: `sm self reviews` names it when a change
reaches its code. Review happens once per promoted commit (section 8, "The review gate"), not per command at run time.

`sm self plugins` prints the loaded stack with each plugin's position (base, middle, outermost), hooks and
wrappers. Proof: `examples/toy` (a project plugin with a pipeline and one native write command) and `examples/layer`
(a layer that adds a gate, a step and a wrapper and replaces one trap); `examples/layer/tests/test_layer_stack.py`
runs `sm --help`, bare `sm`, `sm self reviews` and the write command for the core alone, core + toy,
and core + toy + layer; `examples/layer/tests/test_layer_on_heinzel.py` loads the layer alone and outside the
Heinzelmännchen plugin (`plugins = ["heinzel", "layer"]`): load order, both settings tables, the layer's trap and gate
next to heinzel's, its wrapper vetoing a step before heinzel or the core sees it, and an outer plugin's extra
replacing heinzel's for a core command; `tests/test_core_layers.py` checks each rule above with three in-process
plugins; `tests/test_core_commands.py` checks the core's commands alone, with extras, and replaced by a plugin
command.

**A plugin that does not load is never replaced by the core silently.** Every command except `sm self ...` prints
two loud lines on stderr and exits 70. The core alone runs only on purpose (`plugins = ["none"]` or
`STEUERMASCHINE_PLUGIN=none`).

### Settings (built: `config.py`, pydantic-settings)

One typed model, layered. Precedence, highest first:

1. command line (`--format`, `--project DIR`)
2. environment: `SM_FORMAT`, `SM_PROJECT__PLUGIN`, `SM_ALARMS__STALL_MINUTES`, and for the plugin
   `SM_<PLUGIN>__<FIELD>` (`SM_HEINZEL__ADMIT_LIVE=1`). `STEUERMASCHINE_PLUGIN` is an alias for one release.
3. the project's `steuermaschine.toml`: the nearest one from the current directory up
4. the user's `~/.config/steuermaschine/config.toml`: the default project when no project file is found
5. defaults

```toml
[project]
name = "toy"            # names the state directory
plugin = "toy"
base = "origin/main"

[lanes]
default = 1

[alarms]
stall_minutes = 15

[toy]                   # validated by the plugin's settings model
world = ".sim/world.json"
```

`sm self config` prints every effective value with its source: the core settings, the core tables `[github]` and
`[sonar]`, every plugin table, and the process variables the core reads besides `SM_*` (`STEUERMASCHINE_HOME`,
`STEUERMASCHINE_USER_CONFIG`, `STEUERMASCHINE_PROJECT`, `STEUERMASCHINE_RELEASE`, `STEUERMASCHINE_AGENT`, the XDG
directories), which are one typed model too (`config.Environment`). A secret is a `SecretStr` and is printed
masked; a token is read only from the variable a setting names (`config.secret`).

The core reads the environment in `config.py` only; `tests/test_core_settings.py` enforces it and lists the few
reads left in native plugin modules (inside reviewed units). Handing the environment on to a child process is not
a settings read. The moved libraries keep their reads until their unit is next reviewed; `bin/debt.py` counts them.

### Two projects on one machine

State lives in `$STEUERMASCHINE_HOME` (default `~/.local/state/steuermaschine`), one directory per project name:
runs, receipts, locks, briefs. The project is chosen by where the command runs. Two repositories with two plugins
never see each other's runs (tested). A project file wins over the user file.

### Versioned API and conformance kit

`API_VERSION = 1`. A change that breaks a plugin raises it; the core then refuses old plugins with a line that
says which version each side has. `steuermaschine.testing.assert_conformant(plugin, settings)` is the kit a
plugin runs in its own tests. It checks, without running a step: API version and name; a settings model that
forbids unknown keys and describes every field; every provider satisfies its Protocol; unique pipeline ids and
step names; every external step states what it changes and can print its plan; every trap key and gate name
exists; the whole command tree with this plugin loaded passes the help contract; tool map rows fit the model.

## 6. Help contract, built in (built: `helpdoc.py`)

Every command and group starts its help with five lines, in this order:

```
Ship one head: push, open the PR, wait for green CI, merge, tag.
Use when a head is committed in a worktree and should go out.
Changes: state on this machine; without --live nothing else. With --live: push: pushes the branch; ...
Example: sm head ship .
Next: the NEXT line it prints; sm go shows it again
```

Purpose (at most 140 characters), `Use when|before|after|for|at`, `Changes:` ("nothing (read-only)" or exactly
what is written, pushed or dispatched), `Example:` one command to copy, `Next:` the command that usually follows.
For a pipeline command the core writes these lines from the pipeline itself, so `Changes:` cannot drift from the
steps.

- `sm self lint` walks the built command tree, core and plugin, hidden commands included, and exits 1 on any miss.
  It runs no command. Strict: every argument and option has its own help text (letter `A` in the finding), the five
  lines come in their order (`O`), and the example types `sm`, directly or after a pipe (`X`).
- `-h` and `--help` are answered before any command body runs.
- `sm self tree` is the same tree as data.
- `tests/test_core_help_order.py` runs the strict lint over every shipped stack.

## 7. Output and exit codes (built: `models.py`)

One root option: `sm --format text|json COMMAND ...` (also `SM_FORMAT`, or `format` in a settings file).
In `json` every command prints exactly one JSON document on stdout; progress goes to stderr; an error is
`{"error": ..., "code": ..., "hint": ...}` with a non-zero exit. `--format json` also works after the command.

The documents are pydantic models, and the same model backs the text renderer, the JSON output and the published
schema: `sm self schema` lists them, `sm self schema GoReport` prints one. A core command prints through `emit`,
whose model argument is required; a native command returns its model (a list, a union, or None, which prints
`Nothing`). Every model of every native command is in `sm self schema`. `tests/test_core_json_contract.py` walks
the command tree of every shipped project and fails on a command without the contract; a plugin's plain Typer
command (`sm_commands`) fails it too, so a plugin adds commands as `@native` functions. It then runs every command
in json format with a filler value per required parameter (fake providers) and parses stdout with that command's
own model, or with `Error` on a non-zero exit.

| Exit | Meaning |
|---|---|
| 0 | done, or a planned stop (review stop, dry-run stop) |
| 1 | refused: a gate or step said no; the reason and the next command are printed |
| 2 | usage or configuration |
| 3 | an unacknowledged ALERT holds |
| 70 | error inside the tool or a plugin; also: the configured plugin is not loaded |
| 75 | waiting; run the same command again |

A planned stop is exit 0 on purpose: for an agent a non-zero exit reads as failure. The `status` field tells a
stop from a finish.

## 8. `steuermaschine-heinzel`

Everything Heinzelmännchen-specific. The core has no word of it (`tests/test_core_names.py`).

| Part | Where | State |
|---|---|---|
| Native commands: `head`, `ci`, `queue`, `release`, `daemon`, `mini`, `beads`, `doc`, `gate`, `guard`, `timing`, `lim`, `note`, `status`, `tick`, `why` | `native/<area>.py`, logic in `native/<area>_lib/`; registry `native/__init__.py` | built, live; every command `@native` with a result model |
| Pipelines `head admit` and `release cut` | `pipelines/head_admit.py`, `pipelines/release_cut.py`, `pipelines/common.py` | built and reviewed; each step calls a native command through `native.call`, so that command's own writers' lock holds |
| Tool map | `toolmap.py`, registered through `Plugin.tool_map()` | built |
| Traps | `traps.py`, keyed per step | built |

### The two pipelines

```
head admit:   preflight -> packet -> packet review (STOP) -> prepare -> apply review (STOP) -> apply* -> pr* -> queue*
release cut:  pins -> notes -> admit prepare -> admission review (STOP) -> admit apply* -> publish review (STOP)
              -> resume* -> verify
```

Both are resumable and dry by default (section 3). A step is one native command (`head preflight`, `head packet preopen`,
`head prepare`, `head apply`, `head pr`, `queue add`; `release pins`, `release notes-audit`, `release admit prepare`,
`release admit apply`, `release resume`, `release verify`), looked up by name in the registry, never imported: a
change to that command voids that command's verdict, not the pipeline's. Tests replace a step through
`pipelines.common.OVERRIDE`; an override never opens a shut gate. Inputs and their meaning: `sm head admit --help`,
`sm release cut --help`; the release runbook is `docs/RUNBOOK-release-cut.md`.

`sm release plugin NAME` releases a plugin and follows only the run of its own recorded dispatch. A failed run is
never dispatched again by itself; after the fix `--redispatch` dispatches once more, and refuses while the recorded
run has not completed.

### The review gate

One independent verdict per promoted commit. `bin/promote` refuses unless `$SM_STABLE/verdicts/<commit>` (written
by `bin/promote_verdict.py record`, outside every repository) names the exact candidate commit, the commit the live
release was built from as the base the reviewer diffed from, and a reviewer; its last line is `VERDICT: APPROVE`.
Both the candidate's checker and the live release's checker must accept it (only the first release with a checker
is judged by its own copy alone). It checks again right before the switch, in case the live release moved while it
waited. No option or setting skips it.

What the independence check can and cannot do: it refuses a reviewer whose name or email is a git identity of the
commits in between (author, committer, Co-Authored-By), and a receipt whose `recorded-by` ($STEUERMASCHINE_AGENT of
the recording process) is not its reviewer. Every agent commits under one git identity and runs under one uid, so it
cannot tell one agent from another: independence rests on the coordinator dispatching a different agent as the
reviewer. The apply verdict binds to agent ids of the runs that produced the bytes; this check does not. Also
still the candidate's own code: `bin/promote` and `bin/promote_lib.sh`; a reviewer reads every change to
`bin/promote*`, `bin/rollback` and `bin/check` first (`sm self reviews` lists them as GATE CODE). Write code run from
a checkout (`bin/sm-dev`, `uv run`) is not reviewed; only the stable install is. Write commands do not check
anything at run time; they still need `--live` where they write outside this machine. `sm self reviews` tells the
reviewer which write paths the diff reaches (the code a write unit covers: its module, its `*_lib` package, the
first-party files it imports, and every `*_lib` package one of those files is in, whole and with its data files,
because such a package loads modules and reads files by name; `steuermaschine.reviewgraph`). No command declares a
review file, its files or an exemption: `@native` has no such argument. `sm self lint` flags a `writes=` that
disagrees with the Changes line.
The apply verdict of a head or release (`sm review verdict`, `verdicts.require_independent`) is a separate gate.

## 9. Running it without breaking it (built: `bin/promote`, `bin/rollback`)

The live `sm` was broken twice on its first night because development and use shared one checkout. Now:

- `~/.local/bin/sm` runs a **stable install**: a commit built with `git archive` into its own directory with its
  own environment. Nothing in it points at the development checkout.
- `bin/promote` is the only way it changes: clean tree, full test suite, build, smoke list (plugin loaded from a
  directory without a project file and from another repository's worktree; the commands builders need; core alone
  only on purpose; a broken plugin must fail loudly), comparison of read-only commands with the running version
  (differences need `--accept-diff`), refusal while an admission or release step runs, then an atomic switch.
- After the smoke list, the verdict: one independent verdict on the candidate commit, diffed from the live
  release (section 8).
- `bin/rollback` switches back. Development runs as `bin/sm-dev`.

`bin/promote` contains this machine's smoke list. It belongs in the plugin as data (open).

## 10. Command name

`steuermaschine` and the short `sm`, decided by the owner. `command -v sm` was empty on this machine before it
was installed; there is no common Unix command of that name.

## 11. Open decisions for the owner

1. **How strong must reviewer identity be?** Today a name. Recommendation: keep names now; add a rule
   `review.require_other_model = true` (the verdict's model must differ from the author's) as the next step, and
   signed verdicts only when agents on different machines review each other.
2. **Shared store for a fleet: files on a synced directory, a small server, or the forge itself (issues and
   commit statuses as receipts)?** Recommendation: the forge. Receipts become visible where the PR is, and no new
   service has to stay up.
3. **May the core become public?** Decided: yes. The public repository `steuermaschine` holds snapshot commits of a proven
   development commit (MIT license).

## 12. Core providers (built: `steuermaschine/github/`)

GitHub and Sonar ship in the core, behind the Protocols of section 4, so a project has `sm ci` and `sm pr` without
a plugin. Nothing in the package names a project; the repository comes from settings or from `gh` itself.

### The one `gh` client

`steuermaschine.github.Gh(commands, repo="")` is the only place that builds a `gh` command line. Every other module,
core or plugin, calls it and never starts `gh` itself.

| Call | Returns | Use |
|---|---|---|
| `run(args, timeout=None, retry=True)` | `Result(rc, out, err)`, never raises | anything, including writes (`retry=False`) |
| `json(args)` | the parsed document; raises `GhError` with one short reason | code that has a place for a failure |
| `try_json(args)` | `(data, why)` | probes |
| `api(path, params, method="GET", fields=None)` | `(data, why)`, below `repos/OWNER/NAME/` | REST; a non-GET call is never retried |
| `pages(path, params, key=, limit=)` | `(rows, why, truncated)` | listings: at most `max_pages` pages of 100 |
| `repo()`, `shown(args)`, `Gh.why(result)` | repository or why not; a command line for a dry run; a reason | |

Each call has a timeout (30 s by default). A call that fails with a timeout, an HTTP 5xx or a dropped connection is
tried exactly once more. The process runner is the injected `Commands` provider; tests use
`steuermaschine.github.testing.FakeGh`, which answers by what the command line contains and records every call.

### `GitHub`: Forge, CI, and the reader Protocols

`GitHub(commands, repo)` satisfies the core `Forge` and `CIProvider` and two richer read Protocols that the generic
commands are built on (`github/base.py`):

| Protocol | Methods (`!` writes outside) |
|---|---|
| `ForgeReader` | `pr(number or head branch or head commit)`, `open_prs`, `pr_checks`, `why`, `branch_tip` |
| `CIRuns` | `run`, `runs(commit, branch, status)`, `active`, `jobs`, `log_tail` (through the jobs API), `cancel_run!` |
| `QualityProvider` | `pr_quality` |
| also on `GitHub` | `variable`, `variables`, `write_variable!`, `create_pr!` (body from a file), `required_contexts` |

Rules:

- **Every return value is a pydantic model** (`github/models.py`): `PullRequest`, `PullRequestList`, `Check`,
  `ChecksRollup`, `WhyReport`, `BranchTip`, `Run`, `RunList`, `Job`, `RunJobs`, `LogTail`, `FailedReport`,
  `CancelResult`, `Variable`, `VariableList`, `VariableWrite`, `PullRequestDraft`, `CommitStatus`, `QualityReport`.
  The same model is the text (`render()`), the `--format json` document and the schema (`sm self schema NAME`).
  `PullRequest`, `Check`, `Run` and `CommitStatus` carry the fields of the core dataclasses of the same role.
- **A read does not raise for what the world answers.** The state field holds `unknown: <why>`
  (`models.is_unknown`). Only the methods of the small core Protocols raise `ProviderError`, where the return type
  has no room for it: an empty list must never read as "no run is active".
- **Writes are dry unless `live=True`** and return what they would run. `write_variable` is refused unless the
  provider was built with `variable_writes=True`; a dry run prints the length of the value, not the value. A write
  is never retried.
- **Required apart from optional.** `pr_checks` reads the contexts the base branch requires from the rulesets and
  from classic branch protection, adds what `gh` marks required, and reports `missing_required` (required, never
  reported on this head), `red_required`, `pending_required`, and the optional ones apart. `state` is over the
  required contexts; without any, over every check. If the required contexts cannot be read and everything
  reported is green, the state is `unknown`, not `green`.
- **`why` lists every visible cause**, not the first: merged or closed, draft, conflicts, missing required
  contexts, red or pending required checks, behind the base (when the rule is strict or GitHub says so), review,
  unresolved review threads, and `blocked` when GitHub says so and none of these holds.
- **`log_tail`** removes time stamps and colour codes; when the log has `##[error]` lines it ends at the last one,
  because what follows is the runner cleaning up.

### Sonar (optional)

`steuermaschine.github.sonar.Sonar(project_key)` reads the public web API of SonarCloud or another host, read-only:
quality gate with its conditions, technical debt ratio and duplication on new code, coverage where the host has
it, the count of open issues and the first N of them. The HTTP function is injected; one retry on no answer or
HTTP 5xx; a part that cannot be read is `unknown: <why>` and the report's `state` says which. A token is sent only
when the environment variable named in `token_env` is set. Without `[sonar] project_key` the project has no
quality provider and `sm pr sonar` says how to configure one.

### The generic commands (`github/cli.py`)

| Command | Model | Changes |
|---|---|---|
| `sm ci active` | `RunList` | nothing |
| `sm ci runs --commit SHA --branch NAME --status S` | `RunList` | nothing |
| `sm ci failed RUN` | `FailedReport`: failed and cancelled jobs, each with its failed steps and log tail | nothing |
| `sm ci cancel RUN` | `CancelResult` | dry; `--live` cancels the run |
| `sm pr status NUMBER or HEAD`, `sm pr list` | `PullRequest`, `PullRequestList` | nothing |
| `sm pr checks NUMBER` | `ChecksRollup` | nothing |
| `sm pr why NUMBER` | `WhyReport` | nothing |
| `sm pr sonar NUMBER` | `QualityReport` | nothing |

Exit 0 when the question was answered (the state field carries the answer, also "red"), 1 when it could not be
read or a write was refused, 2 for usage. Which provider answers, first match: an object a plugin put into
`Providers.extra` under `forge_reader`, `ci_runs` or `quality`; the project's `forge` or `ci` when it satisfies the
reader Protocol; the core `GitHub` and `Sonar` from settings. A plugin that defines a group `ci` or `pr` of its own
replaces the core's by the merge rule of section 5, unless it lists the path in its native paths.

### Settings

```toml
[github]
repo = "owner/name"       # empty: gh works it out from the current directory
timeout_seconds = 30
max_pages = 3
merge_method = "squash"
variable_writes = false

[sonar]
project_key = "organisation_project"
host = "https://sonarcloud.io"
token_env = "SONAR_TOKEN"
timeout_seconds = 20
```

Environment: `SM_GITHUB__REPO`, `SM_SONAR__PROJECT_KEY`, and so on. Unknown keys are an error.

### Tests

`tests/test_core_github_client.py`, `_forge.py`, `_sonar.py`, `_cli.py` run against fakes: no network, no real
`gh`. `tests/test_core_github_live.py` is opt-in (`SM_LIVE_TESTS=1 SM_LIVE_GITHUB_REPO=OWNER/NAME`) and read-only.

### Open

- The commands are core commands registered by `github.cli.register`; they print through `emit` with their model.
- `core_providers` does not set `forge` and `ci` to `GitHub`; the commands build it themselves from `[github]`.
- `merge`, `tag`, `create_pr --live`, `cancel_run --live` and `write_variable --live` are tested against the fake
  only; none was run against a real repository.

## 13. What is generic and where it lives (definition of done, item 5)

Measured 2026-11-13 on main `f9134cc`; updated on `feat/dod-5-6` (moved to core: the run lock, output limiter, section reader and
window status commands, the CI log reading, the sm process shapes). "Usable without the plugin" means: importable and tested with
`plugins = ["none"]` or another plugin, with no import from `steuermaschine_heinzel` (the core tests import the
plugin nowhere except `test_core_names.py`, which checks the core for Heinzel words).

| Part | Lives in | Without the Heinzel plugin | Evidence |
|---|---|---|---|
| engine (pipelines, steps, gates, traps, runs, receipts) | core `engine.py` | yes: the toy's `head ship` | `tests/test_core_engine.py`, `examples/toy/tests` |
| go contract (`GoReport`, ranking, read-only store, alarms on top) | core `go.py`, `models.py`, `app.py` | yes: bare `sm` in every stack | `tests/test_core_cli.py`, `examples/layer/tests/test_layer_stack.py` |
| GitHub provider (`Gh`, `GitHub`: Forge, CI, readers), `sm ci`, `sm pr` | core `github/` | yes: `ci` and `pr` groups with the core alone | `tests/test_core_github_*.py` |
| Sonar provider (`pr sonar`, `QualityProvider`) | core `github/sonar.py` | yes | `tests/test_core_github_sonar.py` |
| run lock (slots, load and start bar, a reserved slot for the focus head, push lane, holds, worker caps, background runs) and its commands `gate run`, `gate wait`, `gate status` | core `runlock.py`, `background.py`, `commands/gate.py` | yes: default policy below the state directory; the plugin hands in its policy (`gate.POLICY`, `native/lock.py`) | `tests/test_core_runlock.py`, `tests/test_core_background.py`, `tests/test_core_commands.py` |
| output limiter `lim` | core `commands/lim.py` | yes: logs below the state directory; the plugin hands in `guards_lim_dir` | `tests/test_core_commands.py` |
| Markdown by section, `doc section FILE` | core `docsection.py`, `commands/doc.py` | yes, FILE required; the plugin hands in its tracking document as the default | `tests/test_core_commands.py` |
| validation window (line, lease, throwaway branch) and `ci window status` | core `window.py`, `commands/window.py` | yes: the core's `Rules()`; the plugin hands in its window (cap, lease, namespace). Starting runs (`head validate`, `ci window run`) and ending a turn with branch clean-up (`ci window release`) dispatch the project's workflows and stay in the plugin | `tests/test_core_window.py`, `tests/test_core_commands.py` |
| CI log reading: the failing part of a failed-step log, the runs behind failing checks, failing test names | core `github/logs.py` | yes; which checks a rerun names (`faillog.rerun_job`), the failure classes (`ci_lib/cause.py`) and the commands `ci why` / `ci faillog` (their next step is the project's `ci one rerun`) stay in the plugin | `tests/test_core_github_logs.py` |
| process shapes of sm itself (which `ps` lines are sm, what they run) | core `smprocs.py` | yes; the load guard and the governor that use them carry the project's paths and stay in the plugin | `tests/test_core_smprocs.py`, the plugin's live `test_native_guards_smprocs.py` |
| preflight framework (`Preflight`, `PreflightReport`) | core `preflight.py` | yes; the checks are project rules and stay in the plugin | `tests/test_core_preflight.py` |
| alarms (stall alarm, `sm_alarms`, `alarm list/ack`) | core `go.py`, `app.py` | yes; the Heinzel alarm rules stay in the plugin (`native/alarm*.py`) | `tests/test_core_cli.py` |
| help lint (`self lint`, `sm_help_lint_rules`) | core `lint.py`, `helpdoc.py` | yes: lint is clean for all three stacks | `examples/layer/tests/test_layer_stack.py` |
| settings (layers, sources, `self config`) | core `config.py`, `plugins.Stack.validate` | yes | `tests/test_core_cli.py`, `examples/layer/tests/test_layer.py` |
| output models (`Model`, `self schema`, one JSON document) | core `models.py`, `native.py`, `dispatch.py` | yes | `tests/test_core_native.py`, `tests/test_core_cli.py` |
| write paths a diff reaches (`self reviews`) | core `reviewgraph.py`, `native.py` | yes: the toy's `sim reset` is a write unit | `tests/test_core_native.py`, `examples/layer/tests/test_layer_stack.py` |

What is left in the plugin that looks generic, and why it stays:

| Generic part | In the plugin | Blocker | Plan |
|---|---|---|---|
| `ci window release`: ending a turn | `native/window.py` | the turn itself is `Window.release` (core); the command also removes the project's `validation/*` branches only when none of its focused runs is active, and checks the project's dispatch bounds | stays; a core `release` without clean-up would need its own review unit and gain nothing |
| `ci why`, `ci faillog`, `ci watch` | `native/ci_read.py`, `ci_lib/cause.py` | the log cutting is core now; the failure classes, the known-failure registry and the next command (`ci one rerun`) are project rules | stays |
| `guard heavy`, `guard git`, `guard uv`, load guard, governor | `native/guards.py`, `guards_lib/` | project paths (builder worktrees, the management checkout, live tool names) and rules; the hook protocol (`guardhook.py`) and the sm process shapes (`smprocs.py`) are core | stays |

Not generic, stays in the plugin: admission and packets, the merge queue and train, the release recipe, beads,
Mini verification, briefs and handoff, the project's go decisions and alarm rules.
