# Parity: `sm head meta` against the former commit-meta tool

Old: `tools/hz_preflight_ci_meta.py` (97 lines; `hz-preflight-ci-meta [PR|BRANCH]`). New:
`native/preflight_lib/ci_meta.py` and the command `sm head meta WORKTREE [--pr N]` in `native/preflight.py`.

## What was compared (2026-11-12)

The old module was imported by path (read only) and called in one process next to the new one. Nothing old ran as a
process.

| Compared | Result |
|---|---|
| `pattern()`: the schema pattern of the core clone's pyproject | the same regular expression |
| `check_subject` on the last 400 commit subjects of the core's main branch plus 6 constructed ones (too long, not conventional, merge, revert, unknown scope) | identical for all 406 (62 have a problem) |
| `evaluate` on those subjects with four title / branch / pull request combinations (long title with a PR number, bad branch name, no title) | identical lists, including the close-and-reopen advice |

`pattern`, `check_subject` and `evaluate` are unchanged lines (58 of the old 97 lines are in the new file).

## What differs on purpose

1. The argument is a WORKTREE, and everything is read there: the branch, the commits since the merge-base with the
   base, the pyproject with the commit rules, and the core's `scripts/check_confidentiality.py`. The old tool read
   the commits, the rules and the script from one fixed clone under the home directory, whatever was checked out
   there. A pull request is `--pr N` (was: a number as the argument).
2. The confidential terms: the path of the terms file is the setting `preflight_terms_file` (a file outside every
   repository). The command hands it to the core's own checker in the child's environment; it never opens the file,
   and a finding names a term by its number only. When the file does not exist the output starts with a `note:` line
   (the old tool said nothing; the check then passes unless the environment supplies terms, as before).
3. A detached HEAD is checked under its commit id (the old tool then failed on the branch name `HEAD`).
4. Output: `commit meta PASS (N commits[, title])` / `commit meta FAIL (N), fix BEFORE pushing:` with the same
   problem lines and the same closing line. `--format json` prints the model `CommitMeta`.
5. An unreadable pull request refuses with one line (was: a traceback).

## Edges replaced

| Old | New |
|---|---|
| `subprocess.run(["git", ...])`, `subprocess.run(["uv", "run", "--script", ...])` | `providers.commands.run` |
| `gh pr view N --json ...` through `subprocess` | `steuermaschine.github.Gh(commands, repo).json([...])` |
| `CORE`, `REPO` | the worktree argument, the setting `repo`; the base is the setting `preflight_base` |

## Tests

The 4 tests of `tools/tests/test_preflight_ci_meta.py` moved to `tests/test_native_preflight_meta.py` with their
assertions unchanged; the pattern they use is the built-in one instead of the fixed clone's. The command is tested in
`tests/test_native_preflight.py` (text, json, a pull request, a finding, a missing terms file, a detached head);
the words in those tests are invented.

## Not verified

No run against the real terms file or the real checker script: the checker was answered by a fake in every test.
