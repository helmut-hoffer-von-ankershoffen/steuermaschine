# Review: `sm plugin gallery` (bead heinzel-management-jbgo)

Unit: `native/plugin_gallery.py` (and the run lock it calls through `native/lock.py`). `sm self reviews` prints the
digest.

What it writes: it runs the repository's frozen capture command itself, `uv run --frozen --no-build --group tooling
hz-plugin screenshots NAME --json`, in a heinzel-plugins worktree the caller names. It runs under the run lock and never
goes through make. The command rewrites one plugin's `docs/assets/screenshots/`, its README gallery block and its
catalog metadata. It writes the private draft under `.preview/screenshots/draft/` and the log
`.preview/sm-plugin-gallery.log`. When Codex is installed, it sends the draft images to a read-only Codex agent (the
repository's own privacy review). It commits nothing. The read-only half is `sm plugin gallery-check`.

Structural (sm controls it):
- argv: the frozen command, never `--headed`, `--open` or `--review-receipt`; no make, so MAKEFLAGS, OPEN, TOOLING and
  PLUGIN overrides cannot apply.
- environment: `capture_environment` is an allowlist (PATH, HOME, USER, LOGNAME, LANG, LC_ALL, TMPDIR, TERM) plus
  PLUGIN_NAME. Through GIT_CONFIG_COUNT, ONE `pushInsteadOf` with an empty value rewrites every push URL to a
  transport that does not exist. That covers absolute and relative paths, scp-like URLs and every scheme. Tests show
  a real push to an absolute and a relative path failing, and an scp-like push URL being rewritten. git does not apply
  pushInsteadOf to an explicit `remote.*.pushurl`, and a configured `url.X.pushInsteadOf` with a longer prefix beats
  the empty rule (git takes the longest match). So before the run,
  `git config --get-regexp '^(remote\..*\.pushurl|url\..*\.pushinsteadof)$'` (all scopes) must find nothing: a hit,
  or an unreadable config, refuses and runs nothing. Both kinds are tested with a real git config; a plain insteadOf
  does not count, because it cannot override the block. The tooling scan refuses on `pushurl` too.
- the remote-tracking refs are read before and after the run (`git for-each-ref refs/remotes`). Nothing runs when they
  cannot be read. When they changed, the run fails and tells the caller to contact the coordinator.

Drift detection (sm reads text and refuses on a difference, but cannot prove behaviour):
- the Makefile's PLUGIN assignments: the first equals the frozen command, and later ones are only the TOOLING branch.
  The screenshots recipe is the plain call.
- capture.py still contains each needle of `REQUIRED`: the temporary profile `demo_environment(work, ...)`, HOME and
  HZ_* directories in it, `headless=not headed`, the fictional identity, the environment credential backend and
  `scan_text`.
- no module of `src/heinzel_plugin_tools/` mentions a push (`git push`, a quoted "push", `pushurl`).

REVIEW is printed whenever any image changed, whatever the capture's own agent said: an independent privacy review of
the images is required before merge.

The author verified:
- `sm plugin gallery-check google` against heinzel-plugins main 080b5c3: every condition is met.
- the frozen argv runs with the allowlisted environment in check mode (`screenshots clock --check --json`, ok).

Not verified: a full live capture, which is heavy and calls the Codex agent.

Reviewer probes from round 1 (REQUEST CHANGES at 3a52708), now tests:
- PLUGIN with --headed
- a push in an imported module
- OPEN, TOOLING and MAKEFLAGS in the environment

Independent review (rounds 1-4): ~/Code/packets/sm-10x-release-gallery/REVIEW.md. Every finding closed at aaddf27.

REVIEWED: sha256=b72302360431e751b26a8bc6a2f846c4b3a22f4f4e933bf6ac538e4026b0be58
VERDICT: APPROVE

## Re-verdict at integration/sm-batch2 e4f3ec0 (2026-11-14)

Independent reviewer; wrote none of this code. Baselines and shared delta: ~/Code/packets/sm-reverdict-batch2/VERDICTS.md (recorded verbatim by the integrator).

Unit code unchanged since round 4 (aaddf27); new for it is gate lanes' policy: the capture now runs with the rerun refusal and could fall into the light lane only after three green runs at p50 <= 30 s (not realistic for a capture). The push block, the pushurl/pushInsteadOf refusal and the remote-ref compare are untouched. X1 (advisory) applies: a failed capture on an unchanged plugins worktree is refused on rerun with a --force-rerun hint that `sm plugin gallery` does not take; a capture that wrote images changes the dirty hash, so the common case reruns. Merge: plugin_gallery is in both the import list and MODULES of native/__init__.py.
COVERS: plugin gallery
REVIEWED: sha256=266d3e01d689b9bad4544caed72d7ce608f70fc99d3a3dd497731ab2259d988d
VERDICT: APPROVE
