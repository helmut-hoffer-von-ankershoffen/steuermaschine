# predict pair: fix/e2e-daemon-restart-race head 5e409527037b onto da1362306815 (builder-prepared by hz-admit-wave; NOT the independent review)

Members: test(web): wait for the expert answer before counting the command (one commit, web/e2e/daemon-restart-proof.mjs +15/-1, stacked on the Details head 7dc85604b)

- head `5e409527037b01f4fd7ad525bbfc13a39f37b7ae`, PREDICTED base `da1362306815dc5ea47d691e66ff307ac05097a6` (not a real main; bind before prepare), tree `19eb6081b4a90f18de56bd7e84d14c7c38c84e62`
- real main `437bb247d164d5c5140e5acab1518ba6ea54bb3e`; earlier heads merge in order: fix/e2e-details-journeys-2
- cases COLLECTED on the predicted resulting tree

## Predicted main chain
```
da1362306815dc5ea47d691e66ff307ac05097a6 = squash of fix/e2e-details-journeys-2 7dc85604ba06bbb9154313d9883ced6b62cf9bdd onto 437bb247d164d5c5140e5acab1518ba6ea54bb3e, tree cdf43831327ecf82e2ae872dff7522817e1a2861
```

## Overlap with main
```
none
```

## Case collection
```
browser: collected head tree == local focused JUnit; resulting tree identical
python: collected head tree == local focused JUnit; resulting tree identical
```

## Manifest summary
```
head 5e409527037b01f4fd7ad525bbfc13a39f37b7ae
base da1362306815dc5ea47d691e66ff307ac05097a6
tree 19eb6081b4a90f18de56bd7e84d14c7c38c84e62 (merge-tree)
changed (1):
  M web/e2e/daemon-restart-proof.mjs
protected execution changed: none
pyproject.toml: unchanged
production (0): 
repo selector (base scripts/affected.py): bounded; advisory jobs build, commits, dependency-review, e2e-web-2, lint, secrets
selection: DECLARED, delta vs repository selector:
  + python tests/unit/e2e_browser_output_test.py
  + python tests/unit/standards_test.py
  - browser tests/e2e/ai_access_install_ownership_test.py
  - browser tests/e2e/audio_visualizer_test.py
  - browser tests/e2e/auto_update_test.py
  - browser tests/e2e/chat_only_workshop_test.py
  - browser tests/e2e/chat_profile_color_test.py
  - browser tests/e2e/connected_read_test.py
  - browser tests/e2e/daemon_install_ui_test.py
  - browser tests/e2e/doctor_notice_recovery_test.py
  - browser tests/e2e/durable_five_minute_journey_test.py
  - browser tests/e2e/generic_agent_chat_test.py
  - browser tests/e2e/onboarding_team_name_test.py
  - browser tests/e2e/private_skill_mobile_test.py
  - browser tests/e2e/read_set_approval_prototype_test.py
  - browser tests/e2e/scheduled_connected_test.py
  - browser tests/e2e/scheduled_notification_review_test.py
  - browser tests/e2e/source_suggestion_test.py
  - browser tests/e2e/template_scheduled_test.py
  - browser tests/e2e/web_durable_composer_test.py
case_sha256 browser 2f7e45aee547f0701cf1896dc3fe94765633961f5137050d9f8002ed939d708a (junit sha256 acf1a566fb5341b88c54cd1171bc03643cdf23c9918a4f5d2aff4d29de444e99, 3 cases, NOT bound to head by Git: confirm its run)
case_sha256 python ff4e5b2cf60d56420c21c089a9e0a0bb8ee389b8b2aba4d3d7629b93d57afaf0 (junit sha256 c7875b87d977edadd6bd82ee98f8c4d03b0dd76f7e099ddfb81511662bdff5a5, 2010 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 db8026a21a5dd960db2d5e81609375793a2edbe9ca4d44cd17718f0182524519
main_manifest_sha256 3ec69be8ca59449c99ea39c07f7565981528700db62560ede51016e5825f3410
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/e2e-daemon-race/predict/packet.json sha256 a68b955e41a6578f44f5ff0f1daa0e4ab3a314908bdcec645e63fa7f36efd464
```

## Notes for the independent reviewer
## Builder notes on the declared selection (not the independent review)

Release-critical for 0.9.40: a latent driver race that can fail the candidate's web-2 lane. Do NOT dispatch a hosted
validation on this SHA (5e4095270); validation ran on `validation/e2e-daemon-race-5e409527` (e2b96a7e8 = the head + one
EMPTY commit "chore: validation only", same tree).

Head: `fix/e2e-daemon-restart-race` 5e409527037b01f4fd7ad525bbfc13a39f37b7ae, STACKED on the Details head
7dc85604ba06bbb9154313d9883ced6b62cf9bdd (packet e2e-details-2/m-437bb247). Its own commit changes ONE file,
`web/e2e/daemon-restart-proof.mjs` +15/-1. After the Details PR squash-merges, the diff against main is this one file.
This is a PREDICTED packet (`hz-admit-wave predict --after` the Details packet): it must be bound to the real main
after that merge (`hz-admit-wave bind --main <sha>`) before prepare.

### What failed
- Hosted web-2, run 43298540056 (job log ~/Code/tmp-pr-fill/drp/job.log, diagnosis
  ~/Code/tmp-pr-fill/daemon-restart-proof-diagnosis.md): `web/e2e/daemon-restart-proof.mjs:73`, the count of the
  expert command line "hz system daemon restart" was 0 where 1 was expected. 1 of 4 hosted web-2 runs that evening.
- Cause: DaemonSettings renders the Restart button from `useDaemonStatus` and the command line from the separate
  `useExpert` query (DaemonSettings.tsx:120). The driver waited for Restart only and then called a non-waiting
  `count()`. When `/api/system/expert` answers after `/api/system/daemon/status`, the count is 0. Only the expert
  round can fail. Product code is correct; the race is on main already.
- PROVEN locally by forcing the order (scratch copies, not committed: the fixture answers `/api/system/expert` 400 ms
  late): the driver of the parent commit fails with `0 !== 1` (~/Code/tmp-e2e-details/race/forced-old.log); the driver
  of this head passes all six rounds, chromium and webkit (forced-new.log). Not proven: that the hosted failure was
  this order (the hosted log does not name the browser or the timing); it is the only mechanism found.

### The change (driver only, no assertion weakened)
- `expertAnswered(page)`: `page.waitForResponse` for the path `/api/system/expert`, registered BEFORE `page.goto`.
  It carries a no-op rejection handler so that a round that fails earlier reports its own error, not this open wait.
- `commandLines(page, answered)`: awaits that answer; when expert, also waits for the command line to show; returns
  the count.
- Line 73 keeps the same assertion: count equals 1 when expert, 0 otherwise. The 0 case is now stronger than before:
  it is asserted after the answer arrived, where before it could pass only because the answer was late.
- Question for the reviewer: in the non-expert round the count runs right after the response, possibly before React
  has rendered it. With `expert: false` nothing is rendered either way, so a wrong render of the command line could
  in theory appear a frame later and be missed, as it could before this change.
- Shape for Sonar: both helpers are module-level, nesting 2 at most, no new loop. One changed line inside the existing
  round loop still has an `await` (the assertion line, which had one before). Local stand-in (eslint-plugin-sonarjs
  no-nested-functions, cognitive-complexity; core no-await-in-loop): nothing on the new lines except that one await;
  the file's other 22 awaits-in-loop are unchanged lines. The hosted Sonar gate decides.

### Why the selection is declared
- Selector (`scripts/affected.py --base HEAD~1 --json`): pytest [], vitest [], e2e lane web-2, no reason line.
- browser (1 id): `tests/e2e/daemon_browser_proof_test.py`, the only test that runs `daemon-restart-proof.mjs`
  (3 cases: the two driver params, chromium and webkit inside each driver, and one more test of the file).
- python (2 files): the unit tests that read `web/e2e` from disk: `tests/unit/e2e_browser_output_test.py` and
  `tests/unit/standards_test.py`. web: none, the selector returns no Vitest file.
- The Details change of the parent is covered by its own packet (10 browser ids), not repeated here.

### Local evidence on exactly 5e4095270 (clean worktree before and after, macOS Mini, through hz-gate)
- `make lint`: 16 steps passed; `typos` over the file, the diff and the commit message: clean.
- python lane, serial: 2010 passed.
- browser lane, serial (`-n 0`): 3 passed, FIVE rounds in a row (5/5; JUnit of round 1 is the packet's).
  Logs ~/Code/tmp-e2e-details/race/. Five local passes do not measure the hosted rate; the forced run above is the
  evidence for the mechanism.

### Hosted validation, ubuntu-24.04, full web-2 on `validation/e2e-daemon-race-5e409527` (e2b96a7e8)
- Run 43302281394: success, tally 1/1 green (one run; the failure rate was about 1 in 4, so one pass does not prove it).
- The full web-2 lane on this ref also runs the ten Details journeys of the parent head again.

No Python production changed -> mode python-coverage-not-applicable expected.

## Bind to real main f19a3782232cb24b682b3e0f5b094abb96da18fd
```
real main f19a3782232cb24b682b3e0f5b094abb96da18fd first-parent chain to 437bb247d164d5c5140e5acab1518ba6ea54bb3e: f19a3782232cb24b682b3e0f5b094abb96da18fd
main tree cdf43831327ecf82e2ae872dff7522817e1a2861 == predicted main tree
merge-tree(main, head) 19eb6081b4a90f18de56bd7e84d14c7c38c84e62 == predicted
every manifest field except base equals the predicted packet
```
Predicted packet e2e-daemon-race/predict/packet.json sha256 e0848a7ee75c6c5563f5a0058b267cc087936ea481e9b53c10c0452460173679; cases carried from the predicted resulting-tree collection (same tree).
