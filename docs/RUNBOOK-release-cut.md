# Runbook: cutting a core release (heinzel plugin)

Every line here is taken from the `--help` of the command it names; when they disagree, `--help` wins. Placeholders
are in CAPITALS. Which write commands are open or approved right now: `sm self reviews`.

## The main path: `sm release cut VERSION`

`sm release cut` is one resumable pipeline. State is kept per version, `--set` inputs are remembered, and a step done
by hand is adopted. Without `--live` it stops before the first step that writes outside this machine; `--plan` prints
every step and what it changes, and runs nothing.

```
pins -> notes -> packet -> admit prepare -> admission review (STOP) -> admit apply* -> publish review (STOP) -> resume* -> verify
```

`*` writes outside this machine and runs only with `--live`. `STOP`: a different agent must approve a digest.

| # | What | Command |
|---|---|---|
| 1 | Where things stand (read-only) | `sm release status` |
| 2 | Optional: rehearse the candidate's e2e lanes and plugin Consumer checks against main's tip | `sm release rehearse start --consumers` |
| 3 | Prepare the release commit and its pair packet in the hooked release clone (`release.sh`, then `sm head manifest`) | see `sm head manifest --help` |
| 4 | Start the cut (dry) | `sm release cut VERSION --set packet=PACKET.json --set commit_log=LOG --set clone=CLONE --set review_root=ROOT --set intro="WHAT CHANGED FOR USERS"` |
| 5 | Admission review: a different agent reviews the bytes the cut names and records its verdict | `sm review verdict SHA256 --approve --reviewer NAME --notes "..."` |
| 6 | Continue with the approved digest | `sm release cut VERSION --approved SHA256 --live` |
| 7 | Publish review: the same, for the digest the cut names before publishing | `sm review verdict SHA256 --approve --reviewer NAME`, then `sm release cut VERSION --approved SHA256 --live` |
| 8 | The cut resumes the release (`sm release resume`) and verifies it; run the same command again after a WAITING (exit 75) | `sm release cut VERSION --live` |
| 9 | After ACCEPTED: the checklist, and the deferred CI of open PRs | `sm release after VERSION`, then `sm release train resume-ci --dry-run` |

Inputs of the cut (`--set NAME=VALUE`): `packet`, `commit_log` (its last line is `vX.Y.Z`), `clone`, `review_root`
and `intro` are required. `release_review` defaults to `RELEASE-REVIEW.md`. `snapshot` defaults to `yes` (acceptance is
pinned to the tag's public snapshot). `candidate_gate=yes` is the train path: main may be red with known failures, and
the candidate is the gate. `nonconsumers` is the reviewed stuck-queued receipts file, the same for prepare and apply.

Rules:

- **First attempt only.** A failed candidate is never rerun to green. Check it with `sm release candidate VERSION`
  (0 green on the first attempt, 1 failed or green only on a rerun, 75 running). Drop it with
  `sm release drop VERSION --reason "WHY" [--close-pr]` while it is provably before publication, then cut again.
- **`--intro` is the same text as at the cut.** It is used only when the runner itself opens the release PR.
- **An existing release branch is never touched by `release resume`.** Open the PR of a prepared, reviewed release
  branch with `sm release pr VERSION --head SHA` (try `--dry-run` first), never with a plain push and `gh pr create`.

## The release train

`sm release train status | next | plan | clear | resume-ci` shows and drives the batches: the active trains and their
release tails, what merged since a train was cut, the candidate list, and the deferred CI after a tag.

## Plugins

`sm release plugin NAME` releases a plugin whose version on the plugins repository's main is not on PyPI yet, and
verifies PyPI.

## Repair: the steps by hand

The cut runs these commands itself; use them one by one only to repair a stuck step.

| Step | Command |
|---|---|
| pins | `sm release pins REF`: compares the pinned candidate sources with their reviewed copies; exit 1 needs a review |
| notes | `sm release notes-audit --base origin/main`: every member of a combined PR is named in the notes |
| admit prepare | `sm release admit prepare PACKET.json --core-repo WORKTREE --review-root ROOT [--nonconsumers FILE]` |
| digest | `sm head hash PACKET_DIR/prepare.out` |
| admit apply | `sm release admit apply PACKET.json --approved SHA256` |
| resume | `sm release resume VERSION --intro "..."`; every step is checked first, so the same command continues |
| verify | `sm release verify VERSION --homepage HOST --latest` |
| receipts | `sm release repair-receipt`: moves plugin release stamps into their own file (one-time repair) |

Main's CI evidence before any rerun or fix-forward decision: `sm ci main`.
