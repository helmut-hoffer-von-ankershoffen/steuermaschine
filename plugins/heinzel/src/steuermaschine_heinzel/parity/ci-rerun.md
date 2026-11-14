# Parity: `sm ci rerun` against the former batch re-run tool and `--resume` of the deferral script

No run was re-run for this record (forbidden for the port).

| What | How compared | Result |
|---|---|---|
| The plan (order, caps, skips, refusals) | the old tool's 10 tests against the moved module, assertions unchanged | pass |
| The calls of one batch | scripted gh: `pr list` -> `run list` -> (jobs of each active run, once) -> per picked run: jobs read -> `run rerun ID -R REPO [--failed]` | same calls and order as the old source, except one jobs read per active run instead of three |
| Output lines | `capacity: ...`, `rerun #N branch (why): k run(s)`, `skip X: why`, `refusing: ...`, `FAILED rerun ...`, `nothing deferred` | identical text |
| `--all` | scripted gh: two `run list --status ... --json headBranch` -> `run rerun` per recorded run | same calls as `--resume`; same lines |

Side effects, in order: the re-runs -> the list rewritten without the runs that started -> one journal line.

Differs on purpose:
- waiting (a release/* run active, main queued, a main run with queued jobs, macOS busy) exits 75 instead of 0;
- a failed read of PRs or runs refuses (exit 1) instead of a traceback;
- `--resume [--force]` is `--all [--force]`, and has a `--dry-run`;
- the journal line starts with `ci rerun:` and goes to the store's journal, not to the tracking document;
- the queue order is read from the file named by `ci_ops_queue_state_file`; the environment is not read.

Not compared: a real re-run.
