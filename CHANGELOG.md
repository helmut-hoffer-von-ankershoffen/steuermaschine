# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org).
Entries are generated from [Conventional Commits](https://www.conventionalcommits.org) by
[commitizen](https://commitizen-tools.github.io/commitizen/) (`make release`); edit wording by hand before tagging if needed.

## v0.0.2 (unreleased)

### Added

- `sm review dispatch` reads `CLAUDE_CODE_OAUTH_TOKEN` from the macOS Keychain item `claude-code-oauth-token` when the
  variable is unset; the token is held in memory only, never written to a file, plist or log.
- The visible surface: twelve verbs (`sm`, check, ship, review, release, why, status, work, mini, alarm, note, self);
  everything else is hidden and keeps its path for jobs and hooks. Old visible names are stubs (`Renamed: sm NEW`):
  read-only ones run the new command, writing ones exit 2. `sm -h` is one page in the order of a working day.
- The unknown-command resolver: `sm sonar-close` answers `Did you mean: sm review sonar PR` and exits 2.
- One rule for list options: `--issue A,B` and a repeated `--issue` mean the same everywhere.
- `sm why SUBJECT` for a PR, a version, a run id, `main`, a branch or a bead; bare `sm why` for the queue and the release.
- `sm review sonar PR`, `sm ship PR`, `sm ship` for worktrees of other repositories, `sm release VERSION`,
  `sm status --write` (caps the tracking document), `sm alarm list --check` (absorbs `tick` and the asks).
- Trusted call counts (`sm self stats --calls`), `sm self doc`, `sm self tokens`, `sm self tree --all`.
- One journal per subject (PR, version, branch, bead) under `journals/`: pipeline runs, the focus list and the alarm
  acknowledgements live there; the commands that run the queue, the train and the release driver mirror their working
  files into them, and `sm` computes NEXT from GitHub and the journals only. `sm self migrate-state` moves today's
  state files in (`--back` writes them out again). Runs that are history never produce a NEXT.
- `sm note` writes the log of the day (`research/log/YYYY-MM-DD.md`); `sm status --write` renders the state section of
  the tracking document and caps its size.
- `sm release VERSION` refuses a mistyped version and one that is neither in flight, the latest nor the next.
- MIT license.
- PyPI metadata like heinzel's: a PyPI README, keywords, classifiers (Python 3.15, macOS and Linux, typed) and
  project links.
- Python 3.15 or newer is required.
- CI tests Python 3.15 on Linux, and on macOS on main.

### Changed

- README and AGENTS.md rewritten for the public repository; public SonarQube Cloud badges (quality gate,
  maintainability, security, reliability) without a token.
- The release runbook follows `sm release cut VERSION`, with the hand steps as a repair appendix.
- Internal port and migration notes left `docs/`.

### Fixed

- CI: every curl is HTTPS-only, and the deny-list script runs with `--no-build`.
- Regex alternatives grouped explicitly in the CI cause and preflight readers (no behavior change).
- The standard-library-only test of the guard hook ignores `sitecustomize`, which some Python builds load at start.

## v0.0.1 (2026-11-14)

First public release: the `sm` core, the heinzel plugin and the toy and layer examples.
