# Parity: `sm release drop`

Recorded old behaviour: the old step runner WITH the reviewed drop patch applied in a scratch copy
(`tests/record_release_transcripts.py COPY COPY_WITH_DROP_PATCH`, 2026-11-12 23:24 CET). Replay:
`tests/test_native_release_writes.py::test_native_does_what_the_old_step_runner_did`.

| Scenario | Old command line | Native command line | Compared and equal |
|---|---|---|---|
| drop | `0.9.41 --drop-candidate --reason ...` | `sm release drop 0.9.41 --reason ...` | exit 0; the DROPPED line; 6 `gh` reads in order, no write; hold gone, receipt archived with the drop record |
| drop-close-pr | the same with `--close-pr` | the same with `--close-pr` | exit 0; 14 `gh` calls in order: proof, `pr close 812`, DELETE of the branch ref, proof again; the other release's hold remains; the note about other holds |
| drop-published-refused | as drop, PyPI answers 200 | as drop | exit 1; STOPPED on PyPI; no `gh` call; receipt and hold untouched |

Differs on purpose: the closing hint names `sm release resume VERSION`; json adds typed fields (archive, hold, closed
pull requests, branch deleted, other holds remain). The archive path is printed as it is on disk.
