# Parity: `sm ci quarantine flag` against `--digest` of the former quarantine tool

Same digest lines. Side effects, in order: registry read -> run list -> one artifact download per run not yet
remembered -> per streaking bead and test, once a day: the note -> the day's marks saved. Asserted call by call in
`test_flag_makes_exactly_these_calls_in_this_order_and_notes_a_bead_once_a_day`.

Differs on purpose:
- the note is `bd update <prefix><bead> --append-notes "HH:MM <text>"` (what the old task wrapper ran), or the tracker
  provider's `comment` when the project has one; the text is unchanged;
- after the digest lines one line per note says it was written; `--dry-run` says which notes are due and writes nothing;
- the once-a-day marks are in the store; the old marks file is not read.

Not compared: a real bead note.
