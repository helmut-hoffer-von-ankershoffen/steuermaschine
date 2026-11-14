# Port review: `sm ci classify RUN` (the outside classifier)

Slice heinzel-native-status. Written by the porting agent. The verdict line is the independent reviewer's and is NOT
there. Review unit (what the digest of `sm self reviews` covers, stated in code as `files=UNIT`): `native/ci_classify.py`,
every file in `native/ci_classify_lib/`, and `native/ci_lib/faillog.py` (the condenser that decides what is sent).
The command imports nothing else of the CI reads, so a change to them does not void this review.

## 1. Why this is a write command

The old log tool appended an advisory class line when an environment switch was on. That call sends data outside
this machine to a paid service, so it may not live in a read command. Coordinator decision 2026-11-12: `sm ci why`
and `sm ci faillog` never call the classifier; the call is this command, registered `writes=True`, off by default.
The owner's standing rule is that the coordinator never spends money: the setting `ci_classifier` stays off until
the owner turns it on.

## 2. What moved, verbatim

`python3 plugins/heinzel/tests/ci_verbatim.py` recomputes `ci_classify_lib/jev.py` from the old library with the
listed rules and prints every differing line (none). The rules, all of them:

- the module docstring (it named the old command line and environment variables);
- the import line loses `subprocess`;
- `CONFIG` and `configure(**values)` are added: the configuration comes from the caller, not from the environment;
- four environment reads become `CONFIG` reads (state directory, key, mode, URL for tests);
- the keychain read runs through the injected process runner (`CONFIG["run"]`) and only when a keychain item is
  configured;
- the User-Agent and the hint text of the rejected-key log line no longer name the old tool;
- the old command-line `main` (ping, budget, off, on, ask) is not moved.

Unchanged and to be read as moved code, with what the narrow interfaces of the integration contract would forbid in
new code: `urllib` for the one POST, `fcntl` and plain files for the budget (the budget, the kill switch and the
one-hour pause are shared with the old library while both exist: same directory), `time.time` and `time.sleep`
(`_sleep`) for the retry wait, structured log events on stderr (never the key, never the log text).

## 3. New code, to be read in full

`native/ci_classify.py` (about 90 lines). Order of effects of one call:

1. Setting `ci_classifier` off: refuse. Nothing read but the settings. (Test: no process is started at all.)
2. A pull request number, or a repository that is not on the allowlist `jev.ALLOWED_REPOS`: refuse, nothing sent.
3. The setting `ci_classifier_dir` unset: refuse. `jev.configure(...)` from the settings; the kill switch file `OFF`
   in the state directory: refuse, nothing sent.
4. One GitHub read: the failed-step log of the run. No log: refuse.
5. `jev.classify_failure(condensed log)`: reads the key (settings, else the keychain item through the process
   runner), checks and counts the daily budget under its lock file, POSTs `{model, state, questions}` to
   `https://api.typesafe.ai/v1/systemone` with the key as bearer token, at most three attempts.
6. Prints the label (or that there is none), how many characters were sent, and the budget of the day.

What is sent: the condensed failed-step log only (`faillog.condense(faillog.clean(log), 6, 12)`: first error places
and the last 12 lines), at most 96000 characters, of a PUBLIC repository on the allowlist. No token, no settings, no
file of this machine.

## 4. What the reviewer should check

- The refusals of step 1 to 3 come before any key read and any network call (tests in `test_native_ci_classify.py`).
- The allowlist is the library's own hard list, not a setting.
- No setting has a path default: `ci_classifier_dir` and `ci_classifier_keychain_item` are empty until the owner's
  configuration sets them (the old library's directory and keychain item, if the budget and the kill switch are to
  carry over).
- The key never reaches stdout, stderr or the result model (`redact`; the library's moved tests assert it).
- The condensed log of a public CI run can still contain what a test printed; the old tool accepted that for public
  repositories. Decide whether that stands.
- The help states what is sent, to whom, and that each call is billed.
- Parity record: `parity/ci-classify.md`.

## Independent review (main f9134cc)

Reviewer: independent; wrote none of this code. Unit recomputed by `sm self reviews` (4 files).

- Refusals before any key read or network call: setting off (ci_classify.py:75), PR number (:77), repo not on the hard
  allowlist `jev.ALLOWED_REPOS` (:79, jev.py:15,81), no state dir (:81), kill switch (:86). The key is read only inside
  `jev.ask` (jev.py:147) after `wanted()`; no setting has a path default (ci_classify.py:36-38).
- Writes: only its own state below `ci_classifier_dir`: `budget.json` under `budget.lock` flock (jev.py:93-112, capped
  2000 calls / 2M tokens a day, counted before the POST) and `disabled-until` on a 401 (jev.py:163). One POST, at most 3
  attempts, wait capped at 10 s (jev.py:156-173). No GitHub write (one `gh run view --log-failed` read, :88).
- Key never printed: `log` redacts (jev.py:32-44); `Classification` holds no key. Verbatim move confirmed:
  `python3 plugins/heinzel/tests/ci_verbatim.py` -> "jev: moved verbatim".
- No hz-* call, no force push, no auto-merge; one model -> one JSON document.
- Tests: tests/test_native_ci_classify.py, test_native_ci_classify_jev.py pass (2 verbatim tests skip: old dir path).
- Notes, not blocking: the unit excludes `.go.world` (the gh edge it reads through); the condensed public log may carry
  test output (accepted for public repos, as before).

COVERS: ci classify
REVIEWED: sha256=322dc93908de912cc9e1e188177c2b33a042ff6eb23ee1bf0e1e6d044b8330d8
VERDICT: APPROVE

## Independent review, campaign G2 (integration/sm-complete at 479a8e8, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 479a8e8.
The digest now follows imports: ci_classify.py imports `go.world`, which pulls in go_lib, mini, queue_lib, status
and core. Delta 1d69c43..479a8e8 in the unit: ci_classify.py, ci_classify_lib/ and ci_lib/faillog.py unchanged;
go.py (pipeline runs in the World, NEXT checked against the live command tree via new go_lib/live.py), go_lib
probes/decide/views/words (read-only probes of pipeline run states, `ps`/`git rev-parse`), mini.py, moved_map.py,
toolmap.py, and the core (typing, typed Environment, review gate, digest over imports).

- The classify path uses only `go.world()` (providers, settings, read-only store loads); none of the changed go
  code runs on it and none of it writes. The off-by-default switch, the one outbound call and its redaction are as
  approved before (322dc939...).
- No new write, no force push, stash, merge, PR head validation or hz-* call in the delta.
Tests (G2, `sm gate run -- uv run pytest` on the 15 files of this group: ci_classify*, ci_ops_*, dispatch*, rejudge, core github cli/forge, gate units, native, gate structure): 398 passed, 2 skipped, 1 failed: test_the_cores_own_writers_are_under_the_gate assumed `ci cancel` unapproved; fixed in the same branch to patch the review state (gate still asserted); then 8 passed.

COVERS: ci classify
REVIEWED: sha256=13f51ce8cc954d757c64b27115510449b1c392b9ec0b94210124655479f375d0
VERDICT: APPROVE

## Independent re-verdict, campaign G2 (integration/sm-complete frozen at 2d0877b, 2026-11-13)

Reviewer wrote none of this. Digest recomputed with `uv run sm self reviews` in a fresh worktree of 2d0877b; it
matches the campaign list. Delta read: 479a8e8..2d0877b on every file of the unit.

- Unit delta: only core engine.py and native.py (above). ci_classify.py, ci_classify_lib/, ci_lib/faillog.py and the
  go/status/queue_lib files of the digest are unchanged. The off-by-default switch, the one outbound call and its
  redaction are as approved before. No new write, force push, stash, merge, PR head validation or hz-* call.
- Core delta shared by every unit: native.py `EXEMPT_PATHS`/`is_exempt` (an `exempt=` writer skips the gate only
  for `gate run`: the gate is stricter, not looser); `switch_pending(commands, now)` ignores a pending file whose pid
  was reused (process start from `ps -o etime=` later than the file's UTC time + 2 s) and holds when the start
  cannot be read (fail closed); the format matches bin/promote_lib.sh `mark_switch_pending` (`PID %Y-%m-%dT%H:%M:%SZ`);
  `NativeRuntime.commands` (read-only registry lookup). engine.py: `_supersede` marks an older run under that run's
  own store lock (non-blocking flock, Busy = exit 75), pipelines only.
Tests (`sm gate run -- uv run pytest` on 17 files: ci_classify*, ci_ops_*, dispatch*, rejudge, core github
cli/client/forge, gate units, core native, gate structure, queue hzlib): 479 passed, 2 skipped, 0 failed.

COVERS: ci classify
REVIEWED: sha256=c746b92c433ffa32b1ac5eb8777100d0c1de4e67d61c0336498f2aa16817f0b3
VERDICT: APPROVE

## Digest-order re-verdict (integration/sm-complete at 7704ac6, 2026-11-13)

Independent reviewer; wrote none of this code. The only code change since this file's previous verdict is
`git diff 186f7d0 7704ac6 -- src plugins/heinzel/src`: `_imported` in src/steuermaschine/native.py now sorts the
imported files by `_package_path` (the path below the directory holding the top-level package) instead of by
absolute path. 539b7f7..186f7d0 changes no code (review files, plus `review:` in the commit check's allowed prefixes in
pyproject.toml). The bytes and labels of this unit are unchanged; only the order of imported files in the digest moved.
Checked: every package directory in both trees has `__init__.py`, and each top-level package has exactly one root, so
two files never share a key. The order is total and does not depend on set iteration. A git-archive, non-editable
install in a scratch venv gives the same digest as the checkout for all 74 commands, also under PYTHONHASHSEED 1 and
77. The previous verdict stands.

COVERS: ci classify
REVIEWED: sha256=393437d297e1f89347c576b1760d2c59b643442871f841cc4b8e2421c65220c2
VERDICT: APPROVE

## Campaign re-verdict g2 (2026-11-14, independent; reviewer wrote none of this code)

Target: integration/sm-dod frozen at c36bbe1 (baseline 903a1b7, where this unit was APPROVED). Digest recomputed with
`uv run sm self reviews --format json` in a fresh worktree of c36bbe1; it equals the campaign list.

- Unit delta: ci_classify.py now condenses through the core's github/logs.py instead of ci_lib/faillog.py. Compared
  `clean`/`condense` and ERR, NOISE, ANSI, TS against faillog.py at 903a1b7: identical logic (a rename of the loop
  variable and type hints only), so what is sent to the classifier is unchanged in size and content. UNIT drops
  ci_lib/faillog.py; logs.py is in the digest as an import. The rest of the digest moved via shared/plugin modules.
- Shared core delta (every unit): native.py takes `writers.lock` in the project's state directory around each run of a
  non-exempt write command (shared flock; `sm self migrate-state` takes it exclusively, non-blocking with a bounded
  wait, so it never moves state under a writer). The promote-switch check now runs inside the lock: a single run still
  exits 75, a loop still skips the tick; `guarded(True) or 0` keeps the old exit codes. config.child_environment gains
  drop/drop_prefixes (same filtering the callers did by hand), `search_path()`, `variable()`; plugins.old_variable lets
  an old env variable set a setting only when no file or SM_ variable does (source named in `sm self config`).
  helpdoc lint letters O/X, models StateMigration, hookspec sm_state_moves: no runtime effect on this unit.
- Safety: no force push, stash, auto-merge, --admin or hz-* call added; the cancel and rerun allowlists are unchanged.
Tests (`sm gate run -- uv run pytest`, by file; pytest-xdist is not installed in this repo, so without -n 2):
plugin: test_native_merge_ledger, merge_tier_rules, ci_classify, ci_classify_jev, ci_ops_defer, ci_ops_quarantine,
ci_ops_rerun, ci_ops_sonar, dispatch, dispatch_cov_a_focused, dispatch_focused, rejudge, admission_cov_a_focused_admit,
admission_cov_a_focused_admit_run: 451 tests, 2 skipped, 0 failed. core: test_core_github_cli, client, forge, logs, sonar,
core_native, core_statemove, gate_structure: 126 passed, 0 failed.

COVERS: ci classify
REVIEWED: sha256=a65730d616b4b7bcd7fe4b21042ad21f8cdd66483b1c432d79129395911c5fc8
VERDICT: APPROVE

## Re-verdict at abc64a4, group C (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-abc64a4/C.md (recorded verbatim by the integrator).

ci_classify.py, ci_classify_lib/jev.py unchanged. Changed in its digest: go.py, go_lib/{decide,probes,words}.py,
mini.py, queue_lib/edge.py, new updater_hold.py (read-only reader of the updater state), heinzel toolmap.py
(popup-guard row retired). `ci_classify` uses from `world()` only settings, repo, commands and gh; the core_repo_dir
and packet-glob defaults, `held` probe and records_dir refusal do not reach it. Off-by-default, allowlist, kill switch,
daily caps and no-key-read-before-refusal order unchanged. ci_classify tests green.
COVERS: ci classify
REVIEWED: sha256=b531c50fe5b54ab462a40c45b2906701a765ea76ae5b4efb06c9a72d5610530d
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged. The digest moved only through shared core files: config.py (new SM_CALLER variable, read only by the invocation log) and protocols.py (Store.delete; FileStore and MemoryStore implement it, ReadOnlyStore refuses it, its one caller is release drop). Neither changes what this command does. github/sonar.py now returns (0, "unsupported URL scheme") for a non-http(s) URL instead of opening it (leak guards): stricter, the callers already treat status 0 as unreadable. defaults.py adds FileStore.delete only. native/mini.py (updater hold): a held version counts only when it is at least the target, else the plain "does not satisfy" refusal: stricter. toolmap.py: one new row (plugin gallery).
COVERS: ci classify
REVIEWED: sha256=93d66e4bdae827288a2d8274e9cb2a5f8390f4718c9c6c0bc499e47b91350526
VERDICT: APPROVE
