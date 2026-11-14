# Parity: `sm go --view start|next|prs|wait|tick` against the former startup, next-actions, status, wait and tick tools

All five views are read-only and built from the probes `sm go` decides on (one collection per call).

- start: the route, the latest checkpoint and the timer rows of the startup tool's `--resume`; `latest_checkpoint` and
  `timer_rows` moved (tests/views_verbatim.py; the files are settings: go_tracking_doc, go_timers_file, go_codex_home).
  The route names sm commands. The phase, NEXT and TRAP of `sm go` come first.
- next: the ranking (owner's order, `RANK_BY_PRIO`, `RANK_WORDS`), items, ask and paused rows, hourly update and
  renderer moved from the next-actions tool. Sources: main red (from main's runs), the queue's needs-work entries and
  its loop, open asks and paused work of the tracking document, Sonar marks and PRs without owner or stale (from
  `sm status`), the ship pipeline's NEXT (instead of the old release-ready check), the hourly update. Claims of the
  scripted era are read and shown as `watching`.
- prs: the pull request table of `sm status`, line for line (test_native_go_views.py compares them on the recorded
  status fixture).
- wait: the diagnosis ladder of the wait tool (deferred CI during a release, merge holds, failing, check-ready, load,
  pending, cancelled), in sm words, with the buckets, gate hold, merge holds, Acceptance, load and the queue's waits.
- tick: the anomaly lines of the digest that need no write: trains and release tails, alarms, MAIN RED, holds, queue,
  failing / deferred / green-unqueued PRs, PR runs in flight against 5, main CI starved, load, unknown probes; `quiet`.

Differs on purpose (what is not done):
- next does not write the owner-message audit watermark, claims or the hourly-update record (no `--claim`, `--unclaim`,
  `--updated`); owner messages are `sm doc asks`. The PR-state reconciliation of asks with linked PRs, the Sonar coverage
  number (the badge colour stands in), the flakes history lines and the mini-health item are not ported. Exit 0 always.
- wait: known failures and verified deferrals are not told apart from other cancellations; `log`, `gate`, `hosted`,
  `stages` and the velocity log are not ported.
- tick: no CI auto-deferral (the old digest cancelled PR runs when main CI starved: a write inside a read), no
  announcement state, no bead notes, no doctor, web or version probes, no refill and no quarantine lines; `--watch`
  is `sm alarm check`, `--pr` is `sm ci why`, `--triage` is `sm ci triage prs`.
