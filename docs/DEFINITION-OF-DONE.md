# sm: definition of done ("complete and clean")

Owner goal, 2026-11-12: "Get sm complete and clean. The goal is to accelerate by having a perfect sm."
`sm` is steuermaschine: core in `~/Code/steuermaschine`, Heinzelmännchen logic in the plugin `steuermaschine-heinzel`.
The live command is the stable install in `~/Code/steuermaschine-stable`; it changes only through `bin/promote`, with the coordinator's go.

Each item is reported as done, partial or not done. No rounding up.

## Complete
1. No wrapped scripts: `sm tools gaps --wrapped` prints zero. Every former `hz-*` tool is native in the plugin or core, merged into another command, or retired with evidence that no caller remains.
2. Callers repointed: launchd jobs, Claude hooks, skills, agent definitions and scheduled prompts call `sm` only. `~/Code/heinzel-management/tools` then holds no logic.
3. Write paths native and reviewed: admission (incl. the release-main route), merge queue, release and train, hosted dispatch. Each has a port review file, a parity record against the old tool and an independent review verdict before it is switched on.
4. One resumable command per job: `sm head admit`, `sm release cut`, both with review stops; `sm` alone names the phase, the one next command and its trap, and is right about tonight's cases (declared focus, predict→bind packets, a running prepare).
5. Generic parts in core, usable by another project without the Heinzelmännchen plugin: engine, go contract, providers for GitHub and Sonar, run lock, validation window, preflight framework, alarms, help lint, settings, output models.
6. Layered plugins through pluggy, with the order rule documented and tested by a second plugin.

## Clean
7. Every command follows the help contract; help lint strict: zero findings.
8. Every command honours `--format json` with exactly one document from a typed model; schemas printable with `sm self schema`.
9. No `hz-*` or `hzc` name in any output, help text or state path.
10. Typed settings only (pydantic-settings); no stray environment reads; `sm self config` shows each value and its source.
11. Quality gates for the steuermaschine repository itself, run by one command and by `bin/promote`: ruff, type check in strict mode, tests, coverage of at least 90% lines and branches for core and plugin, typos, no dead code from the wrapping era.
12. Docs current: DESIGN.md, README, the quick reference, the role skills, and a short AGENTS.md.
13. An independent Fable review of the whole tool against this list, with its findings closed.

## Going live
14. `bin/promote --check` green; promote only between release steps; each write path is switched on one at a time, first on a low-risk head, with rollback ready.

## Deleting the old scripts (owner decision, 2026-11-12 22:35, interactive)
"Delete per port": each `hz-*` script is deleted the moment its native `sm` replacement is reviewed and live. 0.9.40 keeps shipping on the current path. Nobody calls a script by hand in the meantime. The coordinator reports the remaining count (`sm tools gaps --wrapped`) at every tick. Deletion order follows promotion: read-only family first, then CI operations, admission, queue, release.
