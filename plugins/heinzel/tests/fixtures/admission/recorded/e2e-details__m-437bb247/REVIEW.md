# preopen pair: fix/e2e-details-journeys head e9c943810ea7 onto 437bb247d164 (builder-prepared by hz-admit-wave; NOT the independent review)

Members: test(web): require the Details disclosure open in chat journeys (one commit, web/e2e/chat-ready.mjs +13/-1, release-critical for 0.9.40)

- head `e9c943810ea75cfad155a431a2f168b2a9559828`, base `437bb247d164d5c5140e5acab1518ba6ea54bb3e` (given main), tree `2644da58a04f7d1db0e15205ff5fc2faae91073b` (git merge-tree)
- cases COLLECTED on the resulting tree (no carried digest); the head-tree collection reproduces the local focused JUnit

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
head e9c943810ea75cfad155a431a2f168b2a9559828
base 437bb247d164d5c5140e5acab1518ba6ea54bb3e
tree 2644da58a04f7d1db0e15205ff5fc2faae91073b (merge-tree)
changed (1):
  M web/e2e/chat-ready.mjs
protected execution changed: none
pyproject.toml: unchanged
production (0): 
repo selector (base scripts/affected.py): e2e lane supervised has no focused lane mapping; advisory jobs build, commits, dependency-review, e2e-supervised, e2e-web-1, e2e-web-2, lint, secrets
selection: DECLARED, delta vs repository selector:
  + browser tests/e2e/chat_only_workshop_test.py
  + browser tests/e2e/chat_profile_color_test.py
  + browser tests/e2e/connected_read_test.py
  + browser tests/e2e/generic_agent_chat_test.py
  + browser tests/e2e/read_set_approval_prototype_test.py
  + browser tests/e2e/scheduled_connected_test.py
  + browser tests/e2e/scheduled_notification_review_test.py
  + browser tests/e2e/source_suggestion_test.py
  + browser tests/e2e/template_scheduled_test.py
  + browser tests/e2e/web_ui_test.py::test_mobile_touch_viewports_keep_navigation_settings_and_chat_reachable
  + python tests/unit/e2e_browser_output_test.py
  + python tests/unit/standards_test.py
  (no bounded derivation)
case_sha256 browser 3c8dffd18661cc3cd41ef99a5ca2a9bcc89f88809bdfda5ac691403e6722aedf (junit sha256 87ad740adcb30ddde85cf7e313490f2b5b83955aac79c1343e7734579818af04, 42 cases, NOT bound to head by Git: confirm its run)
case_sha256 python ff4e5b2cf60d56420c21c089a9e0a0bb8ee389b8b2aba4d3d7629b93d57afaf0 (junit sha256 c7875b87d977edadd6bd82ee98f8c4d03b0dd76f7e099ddfb81511662bdff5a5, 2010 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 75fa446503789471e7945361f6e4ed94d285a307b00655624e94cc4979403f7e
main_manifest_sha256 35727f5f974ffc350f688c89ca99efbf0347f2050a268086bde7daa368255b7b
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/e2e-details/m-437bb247/packet.json sha256 1ac85db9dab82f5482c367b263e65317d57f37cda6371e7161a8ada2ec72dc75
```

## Notes for the independent reviewer
## Builder notes on the declared selection (not the independent review)

URGENT, release-critical: the 0.9.40 candidate failed one browser journey on hosted Ubuntu. Do NOT dispatch a hosted
validation on this SHA (e9c943810): a hosted validation run on the exact head from another ref attaches merge-gate and
coverage-floor to the wrong check suite (this blocked #790). Validation runs use the separate ref
`validation/e2e-details-e9c943810` (head 461520ac1 = e9c943810 + one commit "chore: validation only").

Head: `fix/e2e-details-journeys` e9c943810ea75cfad155a431a2f168b2a9559828 = main 437bb247d + ONE commit, ONE file,
`web/e2e/chat-ready.mjs` +13/-1. No product file, no test file, no workflow, no Python.

### What failed
- 0.9.40 candidate, hosted Ubuntu, lane web-2 (log: ~/Code/tmp-e2e-details/job.log): `1 failed, 23 passed`.
  `tests/e2e/connected_read_test.py::test_mounted_connected_read[webkit]`, 37.4 s, at `web/e2e/connected-read.mjs:21`:
  `locator.selectOption: Timeout 30000ms exceeded`, waiting for
  `getByRole('region', { name: 'Connected read review' }).getByLabel('Read operation')`: the select resolved, but
  "element is not visible" for all 30 s.
- The startup dump of the same failure: creation sent, status 200, Chat selected, `?chat=` in the URL. So the Heinzel
  existed and the pane was shown; the review section was in the DOM and hidden. The section sits inside the Details
  disclosure, which `startChatJourney` opens through `openDetails` (one unverified click on main).
- The [chromium] case of the same test passed in the same run.

### Cause: NOT proven
- The investigator's conclusion, which this packet keeps: a timing race between the one click on the Details summary and
  the conversation still loading. Root cause unproven. The hosted failure was not reproduced on the Mini:
  `local1.log` (2 passed), `stress1.log` (4 x 1 passed under load), `probe2.log` (2 passed).
- What the probes do show (`probe1.log`, `probe3.log` to `probe6.log`; instrumented drivers, scratch, not committed):
  with Details open every ancestor of the select is visible (probe3), so a closed Details is the only state found that
  makes that select "not visible"; and when a press does not land on the summary nothing opens (probe5 webkit:
  `hit ["DIV","?"]`, `openAfterStaleClick false`). They do not show WHY the hosted press missed.
- Not determined: which element took the hosted press, whether the summary moved or was covered, and why only WebKit on
  Linux. A hosted diagnostic dispatch was refused while the release run was active (`dispatch1.log`).

### The change (driver helper only)
`openDetails(page)` in `web/e2e/chat-ready.mjs`:
1. waits for the Details summary (as before, 30 s);
2. NEW waits for the composer "Message your Heinzel" (the conversation has loaded), 30 s;
3. NEW reads `details.open` only after two animation frames (after paint);
4. NEW clicks while it is not open, at most 3 presses (was: one click, never checked);
5. NEW asserts `Details did not stay open` instead of letting a later step wait 30 s on a hidden section.
It no longer clicks when Details is already open (same as before).

Questions for the reviewer:
- Is this a weakened test? Builder view: no. Nothing is asserted less; the helper now fails at the step that broke, with
  its own message. But it IS a retry around an unexplained miss: if the product can swallow a person's first press on
  Details, this helper hides it from these ten journeys. No journey asserts "one press opens Details". Residual risk,
  stated, not closed; a bead for the root cause should stay open.
- Can a retry close what the first press opened? Only if `open` turns true later than two frames after the click
  resolved; the loop would then press again and close it, and the final assert (or the third press) decides. Not seen
  locally.
- An earlier variant, 63dc43c20 (never pushed to this branch; Enter on the summary instead of a click), failed locally:
  `tests/e2e/chat_profile_color_test.py` with `Details did not stay open` (1 failed, 41 passed,
  `fix-local.log`). It was replaced by the click variant (this head): `fix-local2.log` 9 passed, `fix-local3.log`
  33 passed. That failure shows the new assertion fires; why Enter did not open Details there is not determined.

### Why the selection is declared, not derived
- Selector at this head (`scripts/affected.py --base origin/main --json`): pytest [], vitest [], e2e lanes supervised,
  web-1, web-2; reason "web/e2e/chat-ready.mjs: no tests/e2e owner found, all browser lanes". `supervised` has no
  focused lane mapping, so the selection is declared.
- browser (10/10, the cap): `openDetails` is reached from exactly ten drivers, each owned by one id:
  direct callers chat-profile-color, generic-agent-chat, mobile-responsive (the phone sweep node id), read-set-approval,
  scheduled-connected-read, template-scheduled; through `startChatJourney` / `revokeAfterReload` connected-read and
  source-suggestion; through `startChatJourney` / `runDaemonTurn` scheduled-notification-review; through
  `createHeinzelWithMessage` chat-only-workshop and template-scheduled. `grep -l chat-ready web/e2e/*.mjs` gives these
  ten and no other; durable-composer.mjs and durable-five-minute.mjs do not import the helper. No supervised test
  imports it.
- python (2 files): the unit tests that read `web/e2e` from disk: `tests/unit/e2e_browser_output_test.py` (globs
  `web/e2e/*.mjs`) and `tests/unit/standards_test.py` (every tracked file). Checked and left out:
  `docs_style_test.py` and `branding_test.py` read `web/src`, `src` and `docs`, not `web/e2e`.
- web: none. The selector returns no Vitest file; `web/e2e/chat-ready.mjs` is not a Vitest file and nothing under
  `web/src` imports it.

### Local evidence on exactly e9c943810 (clean worktree before and after, macOS Mini, through hz-gate)
- `make lint`: 16 steps passed (lock, format, ruff, contracts, template, docs, design, demo, attributions, zizmor,
  actionlint, typos, docs-check, confidential, web-lint, web-client). `typos` over the changed file, the diff and the
  commit message: clean. Log ~/Code/tmp-e2e-details/admit/lint.log.
- python lane, serial (`-n 0`): 2010 passed. ~/Code/tmp-e2e-details/admit/python.log
- browser lane, serial (`-n 0`, as hosted runs it): 42 passed in 421 s (chromium and webkit params; the phone sweep with its sections). ~/Code/tmp-e2e-details/admit/browser.log
- Local runs are macOS; the failure is Linux WebKit. A local pass is NOT evidence that the hosted race is gone.

### Hosted validation (running when this packet was written; results to be filled in by the coordinator)
- Focused run 43292524197 on `validation/e2e-details-e9c943810`: RESULT: ____________________
- Full web-1 and web-2 suites on `validation/e2e-details-e9c943810`: RESULT: ____________________
- One hosted pass does not prove a race fixed; say how many rounds passed.

No Python production changed -> mode python-coverage-not-applicable expected.
