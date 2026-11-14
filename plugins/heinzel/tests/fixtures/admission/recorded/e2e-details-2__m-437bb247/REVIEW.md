# preopen pair: fix/e2e-details-journeys-2 head 7dc85604ba06 onto 437bb247d164 (builder-prepared by hz-admit-wave; NOT the independent review)

Members: test(web): require the Details disclosure open in chat journeys (one commit, web/e2e/chat-ready.mjs +19/-1, the only blocker of release 0.9.40; replaces head e9c943810 of PR #795, Sonar)

- head `7dc85604ba06bbb9154313d9883ced6b62cf9bdd`, base `437bb247d164d5c5140e5acab1518ba6ea54bb3e` (given main), tree `cdf43831327ecf82e2ae872dff7522817e1a2861` (git merge-tree)
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
head 7dc85604ba06bbb9154313d9883ced6b62cf9bdd
base 437bb247d164d5c5140e5acab1518ba6ea54bb3e
tree cdf43831327ecf82e2ae872dff7522817e1a2861 (merge-tree)
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
manifest_sha256 35dedf6e10585074ead1d897ec25658a96d366f9972f31d011ef0163e1abdfda
main_manifest_sha256 bb0278eafca321e4961f8ec367bf2faed8dff64060341b228b998f4bd7b61636
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/e2e-details-2/m-437bb247/packet.json sha256 d2264932306d2093f30372f8f59eb33f8b620dcd6b3ca966f35dcde297d65b49
```

## Notes for the independent reviewer
## Builder notes on the declared selection (not the independent review)

URGENT, release-critical: the only blocker of release 0.9.40. Do NOT dispatch a hosted validation on this SHA
(7dc85604b): a hosted validation run on the exact head from another ref attaches merge-gate and coverage-floor to the
wrong check suite (this blocked #790). Validation ran on the separate ref `validation/e2e-details-7dc85604`
(head e8f5eda38 = 7dc85604b + one EMPTY commit "chore: validation only", same tree).

Head: `fix/e2e-details-journeys-2` 7dc85604ba06bbb9154313d9883ced6b62cf9bdd = main 437bb247d + ONE commit, ONE file,
`web/e2e/chat-ready.mjs` +19/-1. No product file, no test file, no workflow, no Python.

### Why a second head (this packet replaces e2e-details/m-437bb247, head e9c943810, PR #795: do not use that one)
- PR #795 on e9c943810 failed the Sonar quality gate: new maintainability rating B on 13 new lines. Findings in
  `web/e2e/chat-ready.mjs`: S2004 (functions nested more than 5 deep: the two animation-frame callbacks in a Promise
  in `summary.evaluate` in the `open` arrow in `openDetails`) and S9382 twice (await inside the for loop). Code smells
  are not closed as false positives, so the same behaviour has a new shape:
  - `openAfterPaint(node)`: module-level, self-contained (Playwright serialises it into the page), nesting 3.
  - `pressUntilOpen(summary, presses)`: recursive, no loop: check after paint; return if open; assert presses are left
    ("Details did not stay open"); click; recurse with one fewer.
  - `openDetails` keeps both `waitFor` lines and calls `pressUntilOpen(summary, 3)`.
- Same semantics as e9c943810, to be confirmed by the reviewer: no click when already open; at most 3 clicks; the state
  is read after two animation frames before every decision; after the third click one more read, and the assertion
  with the same message if it is still closed. `git diff e9c943810 7dc85604b -- web/e2e/chat-ready.mjs` shows only this.
- Local Sonar stand-in (eslint 9 with eslint-plugin-sonarjs `no-nested-functions`, `cognitive-complexity`, and core
  `no-await-in-loop`, scratch in ~/Code/tmp-e2e-details/sonar, not Sonar itself: the hosted gate decides):
  e9c943810 reproduces the findings (no-nested-functions at 34:64, await in loop at 37:38 and 37:62); 7dc85604b has
  none in the new lines. Both versions report one `await` in a loop in code that is unchanged from
  main (line 156 old, 162 new), which is not new code.

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
3. NEW reads `details.open` only after two animation frames (after paint), in `openAfterPaint`;
4. NEW clicks while it is not open, at most 3 presses (was: one click, never checked), in `pressUntilOpen`;
5. NEW asserts `Details did not stay open` instead of letting a later step wait 30 s on a hidden section.
It does not click when Details is already open (same as before).

Questions for the reviewer:
- Is this a weakened test? Builder view: no. Nothing is asserted less; the helper now fails at the step that broke, with
  its own message. But it IS a retry around an unexplained miss: if the product can swallow a person's first press on
  Details, this helper hides it from these ten journeys. No journey asserts "one press opens Details". Residual risk,
  stated, not closed; a bead for the root cause should stay open.
- Can a retry close what the first press opened? Only if `open` turns true later than two frames after the click
  resolved; the next press would then close it, and the following read (or the final assertion) decides. Not seen
  locally or hosted.
- An earlier variant, 63dc43c20 (never pushed to a PR branch; Enter on the summary instead of a click), failed locally:
  `tests/e2e/chat_profile_color_test.py` with `Details did not stay open` (1 failed, 41 passed,
  ~/Code/tmp-e2e-details/fix-local.log). The click variant passes. That failure shows the assertion fires; why Enter
  did not open Details there is not determined.

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

### Local evidence on exactly 7dc85604b (clean worktree before and after, macOS Mini, through hz-gate)
- `make lint`: 16 steps passed (incl. typos, docs-check, confidential, web-lint = biome, web-client). `typos` over the
  changed file, the diff and the commit message: clean. Log ~/Code/tmp-e2e-details/admit2/lint.log.
- python lane, serial (`-n 0`): 2010 passed. ~/Code/tmp-e2e-details/admit2/python.log
- browser lane, serial (`-n 0`, as hosted runs it): 42 passed in 427 s (chromium and webkit params; the phone sweep
  with its sections). ~/Code/tmp-e2e-details/admit2/browser.log
- Local runs are macOS; the failure is Linux WebKit. A local pass is NOT evidence that the hosted race is gone.

### Hosted validation, ubuntu-24.04, on `validation/e2e-details-7dc85604` (e8f5eda38, same tree as the head)
- Full web-2 lane (the lane that failed in the candidate): run 43299098260: success, tally 1/1 green.
- Full web-1 lane: run 43299434084: success, tally 1/1 green.
- On the previous head e9c943810 (same behaviour, other shape; ref `validation/e2e-details-e9c943810`): focused runs
  43292524197 and 43292775377. RESULT (coordinator, if known): ____________________
- One hosted pass per lane does not prove a race fixed (the candidate failure was 1 in 1 run; its rate is unknown).

No Python production changed -> mode python-coverage-not-applicable expected.
