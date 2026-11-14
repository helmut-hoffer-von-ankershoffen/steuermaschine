# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [Semantic Versioning](https://semver.org).
Entries are generated from [Conventional Commits](https://www.conventionalcommits.org) by
[commitizen](https://commitizen-tools.github.io/commitizen/) (`make release`); edit wording by hand before tagging if needed.

## v0.0.2 (unreleased)

### Added

- MIT license.
- PyPI metadata like heinzel's: a PyPI README, keywords, classifiers (Python 3.12 to 3.15, macOS and Linux, typed) and
  project links.
- CI tests Python 3.12, 3.13, 3.14 and 3.15 on Linux, and Python 3.12 on macOS on main.

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
