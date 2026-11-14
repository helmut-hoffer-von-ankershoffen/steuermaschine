# Parity: `sm head select` against the former test selector

Compared on 2026-11-12 against a core worktree (read-only), old tool and new command with the same arguments:
- `--self-test`: byte-identical output (20 cases; the same one case fails in both on that worktree: "e2e python test").
- text and JSON for: a kernel file; a docs file; two web files; a module file plus a unit test; a desktop file plus an
  unknown path; no file. Text byte-identical; JSON identical after removing the three fields the model adds
  (`repository`, `self_test`, `failures`).
- `--base origin/main`: identical.
Everything from `JOBS_FULL` to `classify`, `render` and `CASES` is the original code (`tests/coord_verbatim.py`; one
meant difference: an unusable checkout raises instead of exiting).
Differs on purpose: no default checkout path in code (`--repo`, else the settings `select_repo_dir`, `core_repo_dir`);
`--json` is the root option `--format json`; exit 2 with one line for a directory that is no core checkout.
As before, the selector imports the checkout's own `scripts/related_tests.py` (it runs that module of the checkout).
