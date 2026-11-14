# Runbook: cutting a core release by hand (state of main e95c8db plus branch integration/admission-route)

Sources: `CUT_STEPS` in `native/release.py`, the runner `native/release_lib/runner.py` (`release_core`), and the `--help`
of each command named here. Placeholders are in CAPITALS. Anything not confirmed from `--help` or source is marked
UNCONFIRMED. Review state: `sm self reviews`.

Review state of the write commands used (main, digest 715a1eff...): `release resume`, `release plugin`, `release drop` are
APPROVED. `release admit prepare|apply` exist only on the branch `integration/admission-route` (verdict file
`reviews/release-admit-apply.md` approved at 216c11b, digest 53b56f03...; on that branch the unit shows `open` until the
verdict file is merged, and `head apply` is stale there until its re-review). `release cut`, `release train`, `release
rehearse` are MISSING in `sm tools gaps` (no command); the train is not available.

## Known hazards (from the independent review of release resume)

1. Stale release PR (fixed on feat/sm-release-recut). `release_pr(tag)` now takes the OPEN PR (two open PRs refuse), else the
   MERGED one; a CLOSED PR of a dropped candidate is ignored, so `release resume` opens a fresh PR and `sm release pr` works.
   `sm release drop` also archives the saved state of `sm release cut VERSION` (store kind `runs.release.cut.dropped`), so a
   later cut starts fresh. `sm release cut` builds the release-main-only/v1 packet itself (step `packet`, inputs
   `commit_log`, `release_review`); the hand script is not needed.
2. `--intro` is ignored when a release PR for the tag already exists: `open_release_pr` returns at once. It is used only
   when the runner itself opens the PR.
3. An existing release branch is never touched by `release resume`: it stops and preserves it. Open the PR of a
   prepared, reviewed release branch with `sm release pr VERSION --head SHA` (step 9a), never with a plain `git push`
   plus `gh pr create`. There is no `--release-dir` option: the release checkout is the setting `release_dir`.
4. First-attempt rule: a failed candidate is never rerun to green; drop it and cut again.

## Before the cut (read-only)

| # | Command | Changes | Precondition |
|---|---|---|---|
| 1 | `sm release status` | nothing | none; prints the release in flight, PR, candidate CI, freeze, next step |
| 2 | `sm release pins origin/main` | nothing (reads the core checkout with git) | setting `release_core_checkout` points at the hooked release clone; exit 1 = a pinned candidate source differs from its reviewed copy: needs an independent review, then replace the file in `release_lib/approved_sources/` (that IS the re-pin and voids the release verdicts) |
| 3 | `sm release notes-audit --base origin/main` | nothing (git on the release checkout, setting `release_dir`) | run before the release PR is pushed; names combined-PR members missing from the notes |
| 4 | `sm head preflight` (args: see `sm head preflight --help`) | nothing | on the release head; UNCONFIRMED whether it applies to a release commit |

## Admission of the release commit (release-main route)

Live since the release of `dcbc954`: `sm release admit --help` lists two commands, `prepare` and `apply` (both
approved, `reviews/release-admit-apply.md`). Arguments and texts below are copied from the live `--help`.

| # | Command | Changes | Precondition |
|---|---|---|---|
| 5 | `sm head manifest TARGET --base MAIN_SHA --selection SEL.json --junit python=COLLECTED.xml --output PACKET.json` (then `sm head manifest --verify PACKET.json`) | writes the packet file given with `--output`; `--verify` reads only; never fetches, never writes the registry | release commit exists in the owned core checkout; UNCONFIRMED whether the release route needs other arguments |
| 6 | `sm release admit prepare PACKET.json --core-repo WORKTREE --review-root ROOT [--nonconsumers FILE]` | `prepare.out` and `prepare.args.json` next to the packet; one cancel probe per stuck-queued receipt on GitHub. No variable is written | before the release branch is pushed and before its PR is opened, on current main. `--core-repo`: the owned core checkout at the release head; `--review-root`: root the packet's review paths are relative to; `--nonconsumers`: reviewed stuck-queued receipts file |
| 7 | `sm head hash PACKET_DIR/prepare.out` | nothing | then an independent review of that file approves the digest |
| 8 | `sm release admit apply PACKET.json --approved SHA256` | one repository variable (a focused plan slot); pauses and restores the merge queue; writes the attempt journal and a receipt | an independent reviewer approved the sha256 of `prepare.out`. Its help names `sm release cut VERSION` as next; that command is MISSING, go on with step 9 |

## The cut

| # | Command | Changes | Precondition |
|---|---|---|---|
| 9 | `sm release resume VERSION --intro "WHAT CHANGED FOR USERS" --snapshot-acceptance` | see steps below; exit 0 ACCEPTED, 1 stopped or NOT ACCEPTED | stale state cleared (hazard 1); main tip green or `--candidate-gate` (train path: known failures allowed, candidate stays the gate) |
| 9a | `sm release pr VERSION --head SHA --dry-run`, then without `--dry-run` | pushes `release/vVERSION` under the run lock (`sm gate run --`) with the release-only hook selectors, opens `chore(release): vVERSION` with the release notes as body; never merges, never deletes a branch | the branch was prepared in the release checkout (setting `release_dir`) and its exact head SHA reviewed; no tag, no release PR in any state; then step 9 merges it |
| 10 | `sm release candidate VERSION` | nothing | exit 0 = first attempt green; 1 = failed, cancelled or green only on a rerun; 75 = running |
| 11 | `sm release drop VERSION --reason "WHY" [--close-pr]` | removes only the hold this release owns, archives the receipt; with `--close-pr` closes the release PR and deletes its branch | provably before publication: no tag, no GitHub release, PyPI 404, no merged release PR, branch absent, no active run on it. Then cut again from step 9 |
| 12 | `sm release plugin NAME` | dispatches the plugins repository's release workflow, writes the dispatch time in the receipt; after a green run it polls PyPI up to 5 minutes before it stops | a plugin PR with a version bump merged and not yet on PyPI |

Steps of `release resume` in order (the `CUT_STEPS`; each is checked first, so the same command continues): freeze
(takes the merge freeze, or the release-owned hold with `--snapshot-acceptance`) -> main gate (train path, only with
`--candidate-gate`) or main tip green -> release PR open (opens it from `--intro` if none exists) -> previous release
accepted (waits up to `--wait-previous`, default `--timeout`) -> release PR merged (`merge_release_pr`, through the
queue's merge train) -> tag -> merge freeze lifted at tag -> release published -> PyPI -> compose (a failed compose run
is replaced once by a fresh one with the same inputs when plugins main moved past it and a plugin version changed since is
on PyPI; the receipt keeps `compose_replaced`; otherwise it stops) -> acceptance ->
verdict (publishes the acceptance, promotes the release, writes `finished`). Default `--timeout` is 120 minutes per
step. Other actions, one at a time: `--pause`, `--prepare-only`, `--absorb --reason ...`, `--reject-release`,
`--candidate-capacity prepare|request`. Tag-on-green-main (`--refresh`) is a different flow pinned per version; its
runbook is UNCONFIRMED here.

## After ACCEPTED

| # | Command | Changes | Precondition |
|---|---|---|---|
| 13 | `sm release verify VERSION --homepage HOST --latest` | nothing | exit 1 on any FAIL; checks PyPI, core release, app release; add `--mini` for the live service |
| 14 | `sm release after VERSION` | nothing: prints the checklist (resume deferred CI, verify the Mini, close beads, tell the owner) | ACCEPTED printed |
| 15 | `sm release train resume-ci --dry-run` | UNCONFIRMED (named by `release after --help` as next; `release train` is MISSING) | |

## Open or missing

- Open (unreviewed, refuse with exit 70): `beads stamp-released`, `head combine-close` and other coord commands.
- Approved: `release resume`, `release plugin`, `release drop`, `head apply`, `queue add`, `queue drop`, `daemon queue`
  (until a change of their unit voids the verdict: `sm self reviews`). New and open until its review (`reviews/release-pr.md`):
  `release pr`.
- On the branch only: `release admit prepare|apply` (approved verdict file, not merged), batch mode of `head prepare`/`head apply`
  (blocker found at 216c11b, fix and re-review pending).
- Missing: `release cut`, `release train`, `release rehearse`, `ci main` (`sm tools gaps`).
