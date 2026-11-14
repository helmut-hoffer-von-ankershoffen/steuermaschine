# Review: the invocation log and `sm self stats` (core)

Unit: `src/steuermaschine/usagelog.py` (the hook), `src/steuermaschine/statsreport.py` (the report), the call sites in
`src/steuermaschine/app.py` (`run_logged`, `fail`, `self stats`) and `SM_CALLER` in `config.py`. Bead heinzel-management-rlmj.

No registered write command: `sm self stats` is read-only (it reads `invocations.jsonl` and `gate-history.jsonl`), so it
needs no verdict. The logging hook writes one local file of its own, so this file records what a reviewer checks. There is no
verdict line: the author writes none.

What the hook writes: one JSON line per `sm` run into `<state dir>/invocations.jsonl`: command path (found by walking the
command tree, so an argument or option value is never part of it), flag NAMES (`--name=value` gives `--name`, `-n12` gives
`-n`, nothing after `--`), started_at, duration_ms, exit, a reason code on exit 1 or 75 (the leading letter-only words of the
refusal message up to the first colon, at most six; digits and paths are dropped), caller (`SM_CALLER`, 40 characters of
`[A-Za-z0-9._-]`, else `unknown`), the tree SHA of HEAD in the current directory (`git rev-parse HEAD^{tree}` through the one
process runner, 2 s limit, empty when not a repository; not run under pytest), and a failure class (refused, failed, timeout, killed).

What the reviewer checks:
- `record` cannot raise: one `try/except Exception` around everything, `NOTE` cleared in `finally`; a `KeyboardInterrupt` in
  `run_logged` is logged as 130 and still propagates. Tests: `tests/test_core_usagelog.py::test_never_raises`.
- No value is logged: `test_one_line_per_command_with_flag_names_only`, `test_flag_names_never_values`.
- Rotation at 1 MB to `invocations.jsonl.1`, one generation: `test_rotates_by_size`.
- The cost: one `git rev-parse` (a few ms) and one append per command; no network, no lock, no wait.
- No other state is touched; a run without a project logs nothing.

Not done: new `gate-history.jsonl` lines (that file is written by the plugin's lock policy, outside this unit). Merges per day
are not reported: the merge receipts are a plugin's format.

State: approved by an independent reviewer; digest is sha256 over `usagelog.py` and `statsreport.py` (name, NUL, bytes, NUL), no registered command carries this unit.

REVIEWED: sha256=0cd7fb5cb255e008b992726bf15ae44b5b1d3f89e48e05301d1b65519035b808
VERDICT: APPROVE
