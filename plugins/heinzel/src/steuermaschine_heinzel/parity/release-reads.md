# Parity: the read commands of the release area

One record for `release status`, `candidate`, `verify`, `pins`, `after`, `notes-audit`, `assets`. Tests:
`plugins/heinzel/tests/test_native_release_reads.py` (through the command tree, text and json) and the moved tests
`tests/moved/test_native_release_status_summary.py`, `..._chores.py`, `..._assets.py`, `..._notes_audit.py`,
`..._ask_verify_brief.py`.

| Command | Old tool | What runs | Compared | Differs on purpose |
|---|---|---|---|---|
| `release status` | release status screen | moved `active_train`, `counts`, `summarize`, `render`; the three GitHub reads are re-composed on the `Gh` client (same arguments, no shell) | summary and text for no release, a red candidate, `--line`; real train files of 2026-11-12 | hints name `sm` commands; no quarantine line (its module belongs to another slice); json has typed fields |
| `release candidate` | none (new) | core GitHub provider: run, jobs, log tails | red, green rerun, green first attempt, running, unknown target | new command; exit 0 only for a green first attempt, 75 while running |
| `release verify` | published-surface checks | moved `pypi`, `site`, `release` functions | PASS/FAIL lines for the three default surfaces, one surface, a site page | typed options instead of sub-commands chained with `--and`; default surfaces for a version are pypi, core release, app release; `--mini` calls the read-only Mini check of the read-only slice |
| `release pins` | chores, pin-diagnose | moved `cmd_pin` in process | unchanged ref, changed workflow (diff and review request) on a real throwaway git checkout | the review request names the reviewed-files directory instead of the policy source file; json lists each pin |
| `release after` | chores, post-publish | moved `cmd_post` in process | the five steps | steps name `sm` commands |
| `release notes-audit` | none (a step inside the cut) | moved `audit` | unreadable changelog, a dropped combine member, clean | new command surface for an existing check |
| `release assets` | asset probes | moved `assets`, `check`, `wait_stable`, `save`, `digest_line`; the ten lines of `main` are re-composed because its cache path is bound at import | BAD and ok rows, digest, `--stable` | cache path and core checkout from settings; `--stable` sleeps on the clock provider; progress goes to stderr through the runtime |

Not compared: old and new output side by side on live GitHub (no old tool was executed for this record).
