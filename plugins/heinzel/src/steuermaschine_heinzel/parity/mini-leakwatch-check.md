# mini leakwatch check | report | sample

Compared live on 2026-11-12 23:55 for the running product (72 samples in the sampler's file): `report` prints the same
seven lines as the old tool; `check` prints `ok` (old tool: `ok`). The library is moved verbatim
(`tests/ci_verbatim.py`: no differing line after the listed rules); the old tool's 15 test cases moved with it
(`tests/test_native_leakwatch_moved.py`).

| Command | Kind | Side effects |
|---|---|---|
| `report [--hours N]` | read | none; reads the samples file |
| `check` | read | none; with a stale sampler it runs ps, lsof, netstat, launchctl list and one local GET for one live sample |
| `sample` | write (review `reviews/mini-leakwatch-sample.md`) | the same read-only commands, then one appended line in `samples.jsonl` (rotation at 20 MB) |

Differs on purpose:

- `check` does not store the live sample it takes when the newest sample is stale (the old tool appended it). The
  JSON document says `judged_on: live`.
- Thresholds are built in with the values of the old thresholds file; `leakwatch_config_file` overrides single values.
- `leakwatch_state_dir` has no default: every command of the three refuses until the configuration names the
  directory. The daemon label is `io.github.<owner of repo>.heinzel.daemon` unless `leakwatch_daemon_label` is set.
- The sampler's launchd job still calls the old script; pointing it at `sm mini leakwatch sample` is a promotion step.
