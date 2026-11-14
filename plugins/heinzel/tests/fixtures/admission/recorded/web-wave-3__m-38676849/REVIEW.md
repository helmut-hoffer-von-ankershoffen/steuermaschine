# preopen pair: feat/web-wave-3 head ef6172189b73 onto 38676849f2a6 (builder-prepared by hz-admit-wave; NOT the independent review)

Members: chat chain part 2: faces in lists, create as a draft, chat polish and phone, notice look (slices 4-7)

- head `ef6172189b73d67aa0cb011bf2d901390d969978`, base `38676849f2a6bee35c6663984a68afcc7b88892d` (given main), tree `c24e000d061b16401dcbaaa3680ff330dcdf385e` (git merge-tree)
- cases COLLECTED on the resulting tree (no carried digest); the head-tree collection reproduces the local focused JUnit

## Overlap with main
```
none
```

## Case collection
```
browser: collected head tree == local focused JUnit; resulting tree identical
python: collected head tree == local focused JUnit; resulting tree identical
web: collected head tree == local focused JUnit; resulting tree identical
```

## Manifest summary
```
head ef6172189b73d67aa0cb011bf2d901390d969978
base 38676849f2a6bee35c6663984a68afcc7b88892d
tree c24e000d061b16401dcbaaa3680ff330dcdf385e (merge-tree)
changed (22):
  M web/e2e/chat-profile-color.mjs
  M web/e2e/durable-five-minute.mjs
  M web/e2e/mobile-responsive.mjs
  M web/e2e/template-scheduled.mjs
  M web/src/chat/DurableSession.test.tsx
  M web/src/chat/DurableSession.tsx
  M web/src/chat/SessionSwitcher.test.tsx
  M web/src/chat/SessionSwitcher.tsx
  M web/src/heinzel/PersonalHeinzels.test.tsx
  M web/src/heinzel/PersonalHeinzels.tsx
  M web/src/heinzel/look.ts
  A web/src/heinzel/useNoticeLook.ts
  M web/src/i18n.ts
  M web/src/layout/ChatPanel.test.tsx
  M web/src/layout/ChatPanel.tsx
  M web/src/layout/DurableHeinzelStatus.test.tsx
  M web/src/layout/DurableHeinzelStatus.tsx
  M web/src/layout/NotificationCenter.test.tsx
  M web/src/layout/NotificationCenter.tsx
  M web/src/layout/Shell.tsx
  M web/src/notifications/TodayNotifications.tsx
  M web/src/test/copy-standards.test.ts
protected execution changed: none
pyproject.toml: unchanged
production (0): 
repo selector (base scripts/affected.py): repository selector selects the full set; web/src/test/copy-standards.test.ts: shared/sensitive path, selects the full set; advisory jobs commits, dependency-review, secrets, lint, setup, audit, test-linux, coverage, perf, e2e-cli, e2e-supervised, e2e-web-1, e2e-web-2, macos-smoke, macos-e2e, electron, template, core-wheel, consumers, build
web: web/ALL = the whole Vitest suite, no file filter (1 advisory id(s): ALL)
selection: DECLARED, delta vs repository selector:
  + browser tests/e2e/chat_only_workshop_test.py
  + browser tests/e2e/chat_profile_color_test.py
  + browser tests/e2e/connected_read_test.py
  + browser tests/e2e/durable_five_minute_journey_test.py
  + browser tests/e2e/generic_agent_chat_test.py
  + browser tests/e2e/scheduled_connected_test.py
  + browser tests/e2e/scheduled_notification_review_test.py
  + browser tests/e2e/template_scheduled_test.py
  + browser tests/e2e/web_durable_composer_test.py
  + browser tests/e2e/web_ui_test.py::test_mobile_touch_viewports_keep_navigation_settings_and_chat_reachable
  + python tests/unit/branding_test.py
  + python tests/unit/docs_style_test.py
  + python tests/unit/e2e_browser_output_test.py
  + python tests/unit/standards_test.py
  + web web/ALL
  (no bounded derivation)
case_sha256 browser 4cb4d642d6c9beeecf03ab7ec27f8d13fdedfaff660348c2b5957e58f8b663c8 (junit sha256 fb23e0f768a92dcf43f4cde7f2ee72e60fe0840a6be2d8582007e55e9820e77b, 41 cases, NOT bound to head by Git: confirm its run)
case_sha256 python 34e7af47171363d3cc7d0251f26b63c6b3059a77bd2fb32023964081e9dda3e2 (junit sha256 c506a167ab7b5c6399788f53245ad254d9b7acd9faef42eb039a4de40c5ff1c7, 2050 cases, NOT bound to head by Git: confirm its run)
case_sha256 web 29ba4d17363b5b5dee8477b64107651f895888885b7ec11b8616bbc099587910 (junit sha256 1bfd1846d1661a6207cec440ab5bffc502822c0181f8b81d8cc617743f5557c8, 1539 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 52575529c76b4a0854afa1c331956d93d510638089921fd8f8515c4df4422b2e
main_manifest_sha256 c59e88d1f8e12d80ffe20896a90ac14e5605ad49e13e12dc09f1b327f902ccf4
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/web-wave-3/m-38676849/packet.json sha256 5a8ea0704699fc1f9dfceef373b61ae788f765cbb7680976e4bbe32ba7f69230
```

## Notes for the independent reviewer
## Builder notes on the declared selection (not the independent review)

Head: `feat/web-wave-3` ef6172189 on main 38676849f (#791, the chat prefix). It is the second part of the chat chain: the four
slice commits of `feat/chat-create-flow-0912` cherry-picked onto the web-wave-2 head 2da1e0099 (2732c5053 faces in lists,
0aba71253 create as a draft, 8a3fadaaa chat polish and phone, 546cb1efa notice look), then 5f620a3c1 (dedupe, no behaviour
change), 9afb0cfe6 (pane action standard), the merge of main fcffe3490 and ef6172189 (one class on the picker card).
The merge of main: main is the squash of 2da1e0099, so git reported conflicts in seven files this head had already changed on
top of 2da1e0099; all were resolved to this head's side, and the merge tree is byte-equal to 9afb0cfe6
(`git diff 9afb0cfe6 fcffe3490` is empty; an auto-merged duplicate of the `?chat=` effect in Shell.tsx was removed to get there).
Nothing to regenerate: `make lint` (16 steps incl. web-client --check, docs --check, typos, biome ci) passes. No Python changed.
22 files differ from main, all under web/.

All three JUnit files were produced on exactly ef6172189 (clean worktree before and after), macOS Mini, through hz-gate, see
~/Code/tmp-web-wave-3/r4/. Also on this head: typos over the changed files, the diff and the commit messages; tsc --noEmit.
The browser lane ran twice on this head: -n 2 (the packet's JUnit) and SERIALLY (-n 0, as hosted does): 41 passed both times.
Lanes: web/ALL 1539 passed (167 files); python 2050 passed (the 4 files); browser 41 passed.
Do NOT dispatch a hosted validation on this SHA (merge-gate and coverage-floor would attach to that suite and block the PR).

Why the selection is declared, not derived:
- web: `web/src/i18n.ts` (WEB_SHARED) and `web/src/test/copy-standards.test.ts` (web/src/test/ is a FULL path in
  scripts/affected.py) changed -> `["web/ALL"]`, the whole Vitest suite.
- copy-standards.test.ts DECISION (option (a) of FOLLOW-UP-HEAD.md): declared selection against the selector's "full set"
  verdict. The only change to that file is ONE deleted ALLOW row, `durable.createFailed`. Correction to the follow-up note: the
  copy was not reworded; the key is gone from i18n.ts because the footer item no longer creates a Heinzel itself (it opens the
  draft), and the test fails on a stale ALLOW entry ("ALLOW only shrinks"). The row cannot go in an earlier or later PR than the
  key. copy-standards.test.ts itself runs inside web/ALL on this head.
- python: no Python production or test changed; the selector returns no pytest id. Declared the same 4 unit files as part 1,
  which read the changed web files from disk: docs_style_test, branding_test (i18n.ts), standards_test, e2e_browser_output_test
  (scans web/e2e drivers). Searched tests/unit and tests/integration for other readers of the 22 files: only
  tests/unit/affected_test.py names `web/e2e/mobile-responsive.mjs`, as a fixture string, not read from disk; it was run locally
  anyway and passes (not in the selection).
- browser (10/10):
  - 4 ids own the 4 changed drivers: chat_profile_color_test (chat-profile-color.mjs), durable_five_minute_journey_test
    (durable-five-minute.mjs, the create-as-a-draft journey on a phone, chromium and webkit), template_scheduled_test
    (template-scheduled.mjs), and the node id of the parametrized mobile test of web_ui_test.py (mobile-responsive.mjs).
  - 6 ids whose drivers are unchanged but walk what the product change touches (messages are now bubbles with
    `data-message-speaker`, replies are Prose, the composer is one row and empties while a message is on its way):
    web_durable_composer_test, generic_agent_chat_test (holds a press on a reply's disclosure), scheduled_notification_review_test,
    connected_read_test (the 11th owning id of part 1, hosted now as the follow-up note asked), scheduled_connected_test (its
    driver matched the composer text with getByText: it failed on an earlier state of this chain when the message showed both
    in the composer and in the bubble; fixed in the product, the composer is empty while sending), chat_only_workshop_test.
  - OUT of the hosted selection, run locally on this head (extra lane, 5 cases passed): source_suggestion_test,
    read_set_approval_prototype_test, web_ui_test.py::test_mobile_sections_cover_every_viewport_of_the_driver. Reason: their
    drivers are unchanged and they send one message and read a review panel; the same send-and-reply path runs in the six above.
  The other tests of web_ui_test.py and the rest of the browser lanes were not run on this head; the candidate runs the full lanes.

Found by the full suite, not by the focused runs of the source branch (fixed in 9afb0cfe6, no allow-list change):
- `src/lib/paneActionsStandards.test.ts` failed on web/ALL: a second hand-drawn `border-t` in PersonalHeinzels.tsx (the draft
  form), two `justify-end` rows in DurableSession.tsx (the person's bubbles) and raw `<button>` cards and swatches.
  Fix: the draft and the saved profile share one `EditorForm` (one rule), cards and swatches are the shared `Button`
  (role="radio", aria-checked; icon swatches carry aria-label and title), the person's bubble sits right by `ml-auto`.
- ef6172189: on the shared Button, `[&_svg]:size-4` shrank the picker card's figure to 16 px (seen in the screenshots, not by
  any test); the card now sets `[&_svg]:size-10`. Screenshots in this packet are from ef6172189's content (taken with that
  one-line change applied, before its commit).

Sonar (new_duplicated_lines_density gate, 3% of new lines; part 1 failed it once at 3.2%):
- Done up front in 5f620a3c1, no behaviour change: `readProfileSave` validates through `parseDraft` (the 20-line appearance
  check existed twice), one `REQUEST_ID` pattern, the picker tests open the first Heinzel through `openAlpha()` (15 repeated
  two-line openings), and the row's speaker, label and text are the functions `speakerOf`, `speakerLabel`, `MessageText` instead
  of nested ternaries. The drivers keep `recurringTurns` and `createHeinzelWithMessage` from part 1.
- Estimate with jscpd 4 over web/src and web/e2e (token based, test files included, i18n.ts excluded as in
  .sonarcloud.properties), counting only lines added against main that fall inside a clone: at 100 tokens / 10 lines: 0 lines;
  at 50 tokens / 5 lines: 5 of 1178 new lines (0.42%), in two places that were
  duplicates before (the opening of template-scheduled.mjs and one test opening in SessionSwitcher.test.tsx). It is an approximation, not Sonar's number: the hosted gate decides.

Not changed although slice 7 of the review named it: `NoticeHelper`, the stored notice and the native notifier. A notice's
figure is resolved in the web app from the conversation its route links to (`/heinzel?chat=<session>`), with the recorded hat as
fallback. Reason: new fields in the stored helper would be refused by older versions sharing the notification store
(`extra="forbid"`; see tests/unit/modules/notify_legacy_store_test.py for the mixed-version contract). Native notifications
stay hat-only; that is a separate solo change.

Screenshots (shots/, isolated dev instance with its own HZ_* dirs, fictional Heinzels; replies and the notice are mocked in the
browser because that instance has no AI access): see PR-BODY.md for the file list.

Local evidence is macOS with Vitest --maxWorkers=2 and pytest -n 2 (browser also -n 0); hosted lanes are Linux, serial.
Browser cases include chromium and webkit params. No Python production changed -> python-coverage-not-applicable expected.
