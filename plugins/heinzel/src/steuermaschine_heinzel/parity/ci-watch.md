# Parity: `sm ci watch` against the former watch tool (`hz-watch`, 99 lines)

Verdict: NATIVE, complete. Read-only (`writes=False`).

Compared by:
- the former tool's own four tests and the key test, moved with unchanged assertions
  (`tests/test_native_tick_watch.py`: parse of run ids, PR numbers, `plugins 38` and both URL shapes; red PR with the
  failing check and the test names of the failed run only; wait, then green, `review` ignored; run success, run
  failure with jobs and tests, timeout code 2);
- the old tests of the helper functions it took from other tools (`settled`, `test_names`, `read_attempt`);
- the command through the whole tree on a scripted `gh` (text and `--format json`, polls through the clock provider);
- a read-only run against the real repository on 2026-11-12 (no old tool was run): merged PR 796 -> `PR #796: merged`
  exit 0; run 43312360188 -> `run 43312360188: success` exit 0; run 43305436883 -> `failure`, the failed job and one
  test name from its log, exit 1; a pull-request URL in json.

| Behaviour | Result |
|---|---|
| target grammar (number below / above 10,000,000, URL, `plugins 38`, `-R`) | identical (`checks.parse`, 49 of 52 lines of the tool body unchanged) |
| output text: `LABEL: VERDICT`, `  failed: ...`, `  tests:` and one line per test | identical |
| exit codes 0 green or merged, 1 red or closed, 2 timeout | identical (2 is kept as the old tool's; the contract's WAIT code would be 75) |
| poll every 30 s, default timeout 90 min | identical; now settings `tick_poll_seconds`, `tick_watch_timeout_minutes` |
| advisory `review` check ignored, its run never read | identical |

Differs on purpose:
- `--format json` prints one `Watched` document (kind, repository, number, outcome, code, failed, tests, text);
- a target that is no number, URL or known repository word is refused as usage (exit 2); the old tool printed a traceback;
- every `gh` call has a timeout (60 s, the failed log 180 s) and a transient failure is tried once more (the one client);
- the failing test names are read from the log on every call. The old tool went through the flake counter's
  `read_attempt`, which **wrote a cache** (`~/.cache/hz-flakes/<repo>-<run>-<attempt>.json|.log`) that the flake
  counter reads. That write is not ported: this command writes nothing. When `ci flakes` is native it owns the cache
  and `hub.failing_tests` becomes an import.

Duplicates until their slices land (all in `native/tick_lib/hub.py`, each with its old test):
`settled`, `FAILED` (merge train), `failed_runs` (queue library), `failing_tests`, `read_attempt`, `test_names`,
`TEST`, `STAMP` (flake counter). `split_key`, `CORE`, `PLUGINS` are imported from `admission_lib/identity.py`.
