# Review: chore/callers-sm-only at 9e7fe91 (tools gaps: group rows count as native)

Independent reviewer; I wrote none of this change. Base main 491ae68 (merge-base fd499f0).

## The change
- `src/steuermaschine/app.py` `_tools.rows()`: after `paths` (plugin-ported plus core-declared), every command-tree
  group with at least one path below it (`p.startswith(g + " ")`) is added to `paths`. A row naming such a group
  resolves to native. Groups are taken from the real tree (`r["group"]`), and children only from `paths`, so a
  generic core child does not make a group native. Correct.
- `toolmap.py`: hz-sonar-close now points at `ci sonar-close` (it was `doc sonar-close`, which does not exist).
- `tests/test_core_native.py`: a group row `demo` becomes native; the gaps summary counts 3 native.

## Verified against the real tree
`sm --format json tools map` on main vs branch: exactly 5 rows change, all missing -> native:

| tool | row | commands below it (from `--help`) | smoke run |
|---|---|---|---|
| hz-admit-wave | head packet | preopen, predict, bind, wave (= old tool's 4 modes) | help ok |
| hz-ci-one | ci one | dispatch, rerun, watch, status, repeats (= old subparsers) | `status`, `repeats` exit 0 |
| hz-quarantine | ci quarantine | list (incl. `--line`, `--digest`), flag | `list` exit 0 |
| hz-rehearse | release rehearse | plan, status, start, step (old `tick`), consumers, cancel | `status`, `plan` exit 0 |
| hz-sonar-close | ci sonar-close | leaf command | preview runs, asks for manifest |

No other row changed status, so no truly missing tool is hidden and internal/retired rows are unaffected (8 internal,
10 retired before and after). Remaining gaps: `head admit`, `ci flakes`, `mini loops`; none of the three
exists in the tree. The claim of 3 gaps is right (`3 missing, 63 native, 8 internal, 10 retired; 0 wrapped`).

## Write units (`sm self reviews`)
The digest of the release-rehearse unit differs from main (`42f2968...` approved on main, `fa97f72...` open on the
branch). The reason is not this change: no unit's file list includes `app.py` or `toolmap.py`, and the branch does not
touch any of the 13 rehearse files. The branch is based on fd499f0, before main merged fix/rehearse-consumers
(7b2f102, 88d9cde). `git merge-tree` with main is clean, so after the merge the unit has main's files and main's
approved digest. No unit needs a new verdict from this branch.

## Gates
- `sm gate run -- uv run pytest tests/test_core_native.py plugins/heinzel/tests/test_heinzel_plugin.py`: 19 passed.
- `sm gate run -- uv run sm self lint`: "help contract: every command passes".

## Non-blocking notes
- The group rule says "some command below exists", not "the old tool is fully covered". For these 4 rows I checked the
  coverage by hand. A later partial port of a group row would show as native too. Consider a note in the rule's comment.
- Stale user copy that already exists on main: `native/go_lib/words.py:34` says "sm doc sonar-close handles bugs and
  vulnerabilities only". That command does not exist; it should be `sm ci sonar-close` (and the parity replace in
  `plugins/heinzel/tests/test_native_go.py:37` needs the same edit). Good follow-up for this branch's caller sweep.

VERDICT: APPROVE
