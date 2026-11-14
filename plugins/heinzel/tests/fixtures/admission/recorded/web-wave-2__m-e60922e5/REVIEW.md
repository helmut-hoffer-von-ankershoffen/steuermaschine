# preopen pair: feat/web-wave-2 head 2da1e00991e5 onto e60922e51853 (builder-prepared by hz-admit-wave; NOT the independent review)

Members: feat(web): Heinzel chat shows its look, conversation first (identity slices 1-3 + AI access hint fix + journey driver fixes and helpers)

- head `2da1e00991e50bec16f3fadf87c5c60218ea3f0a`, base `e60922e51853bd303a42c5a64385f7028d280520` (given main), tree `878a674f7db6ebd709fbcadd0d62d72a37119406` (git merge-tree)
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
head 2da1e00991e50bec16f3fadf87c5c60218ea3f0a
base e60922e51853bd303a42c5a64385f7028d280520
tree 878a674f7db6ebd709fbcadd0d62d72a37119406 (merge-tree)
changed (20):
  M web/e2e/chat-only-workshop.mjs
  M web/e2e/chat-profile-color.mjs
  M web/e2e/chat-ready.mjs
  M web/e2e/connected-read.mjs
  M web/e2e/durable-composer.mjs
  M web/e2e/generic-agent-chat.mjs
  M web/e2e/mobile-responsive.mjs
  M web/e2e/read-set-approval.mjs
  M web/e2e/scheduled-connected-read.mjs
  M web/e2e/scheduled-notification-review.mjs
  M web/e2e/source-suggestion.mjs
  M web/e2e/template-scheduled.mjs
  M web/src/chat/ConnectedReadSetReview.test.tsx
  M web/src/chat/DurableSession.test.tsx
  M web/src/chat/DurableSession.tsx
  A web/src/heinzel/look.ts
  M web/src/i18n.ts
  M web/src/layout/ChatPanel.test.tsx
  M web/src/layout/Shell.test.tsx
  M web/src/layout/Shell.tsx
protected execution changed: none
pyproject.toml: unchanged
production (0): 
repo selector (base scripts/affected.py): e2e lane supervised has no focused lane mapping; advisory jobs build, commits, dependency-review, e2e-supervised, e2e-web-1, e2e-web-2, lint, secrets
web: web/ALL = the whole Vitest suite, no file filter (1 advisory id(s): ALL)
selection: DECLARED, delta vs repository selector:
  + browser tests/e2e/chat_only_workshop_test.py
  + browser tests/e2e/chat_profile_color_test.py
  + browser tests/e2e/generic_agent_chat_test.py
  + browser tests/e2e/read_set_approval_prototype_test.py
  + browser tests/e2e/scheduled_connected_test.py
  + browser tests/e2e/scheduled_notification_review_test.py
  + browser tests/e2e/source_suggestion_test.py
  + browser tests/e2e/template_scheduled_test.py
  + browser tests/e2e/web_durable_composer_test.py
  + browser tests/e2e/web_ui_test.py::test_mobile_touch_viewports_keep_navigation_settings_and_chat_reachable
  + python tests/unit/branding_test.py
  + python tests/unit/docs_style_test.py
  + python tests/unit/e2e_browser_output_test.py
  + python tests/unit/standards_test.py
  + web web/ALL
  (no bounded derivation)
case_sha256 browser 1cb07661091ab77184caaad1923c09516eea553afc1c53cd63a872745bf04e7f (junit sha256 460517f4723f59f8ae2ee387af6b667a9d8135bdf0914f0e70e362f7b7b58788, 41 cases, NOT bound to head by Git: confirm its run)
case_sha256 python 34e7af47171363d3cc7d0251f26b63c6b3059a77bd2fb32023964081e9dda3e2 (junit sha256 c506a167ab7b5c6399788f53245ad254d9b7acd9faef42eb039a4de40c5ff1c7, 2050 cases, NOT bound to head by Git: confirm its run)
case_sha256 web adf8009e9d6d874a01f776b0c49a2716d54dcb9b4734d038ebf08f7fdd38687d (junit sha256 0f53067e0985420c97e80a5d2a9f59af3f6ebbf82caaf385c17b70dd4d925c98, 1536 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 2a9f4a9bf457a19427e0ee129fd563638838a5c0aebd8478ee6e04f09c2fea03
main_manifest_sha256 8bd303567242edae4753b76e507d5829d1aa5aa1ff0e4da391d468ca40692f48
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/web-wave-2/m-e60922e5/packet.json sha256 bef4d394de169c193d54a3f2f613ef2cc8f78dd7bc395e4cbd60bcbb32ac4995
```

## Notes for the independent reviewer
## Builder notes on the declared selection (not the independent review)

Head: `feat/web-wave-2` 2da1e0099 = 377f9a820 + one comment-only commit, `docs(web): say which journeys share the chat-ready
helpers` (2 comment lines in web/e2e/chat-ready.mjs, no code). Reason for the new head: PR #790 on 377f9a820 was blocked with all
checks green because a hosted validation dispatch had run on that exact SHA from another ref, so merge-gate and coverage-floor
attached to that check suite; the same content needs a new SHA. This packet replaces cbde2023... (head 377f9a820). Do NOT
dispatch a hosted validation on this SHA. All three JUnit files were produced again on exactly 2da1e0099; on this head the
browser lane ran with -n 2, the serial (-n 0) run was on 377f9a820 (identical apart from the two comment lines).
377f9a820 = 7ba7da7b8 + one commit (see "Hosted browser failure" below); 7ba7da7b8 = 334494c39 + one
commit (see "Sonar" below); 334494c39 = chat identity slices 1-3 (81b4d3208, e5fd38b5b, 66a441b92) + 084ecee06 + 3b2ebc6c0
`test(web): phone sweep opens Details before the suggested look`, with main e60922e51 (#788, web-wave-1) merged in. The merge
was clean (git auto-merged web/src/i18n.ts; nothing to regenerate: `make lint` incl. web-client --check and docs --check pass).
The head's diff against main is the same 20 files as before #788; #788 changed no web/e2e driver (only tests/e2e/web_ui_test.py,
where the selected mobile test still exists). All three JUnit files were produced on exactly this head (clean worktree before
and after), macOS Mini, through hz-gate. On this head also: `make lint` (16 steps incl. typos, biome ci), typos over the changed
files, the diff and the commit messages, and tsc --noEmit: all clean.

Hosted browser failure (new in this packet; it replaces b0d90199..., head 7ba7da7b8, which must not be used):
- Run 43206608491 on 334494c39, job "Focused tests (browser)": 2 failed, 39 passed. Failed:
  test_fractional_... and test_partially_clipped_reply_disclosure_remains_clickable_during_workspace_scroll, both [chromium],
  at web/e2e/generic-agent-chat.mjs:239 (the reply's "Tool activity for this reply" disclosure did not open).
- CAUSE, from the hosted disclosure trace in the log (both failures identical): the workspace scroller held still (1486 /
  1481 throughout: the product's press anchoring worked). About 30 ms after pointerdown the INNER pane scroller's content
  shrank by 48 px (scrollHeight 2280 -> 2232) with its scrollTop unchanged (1798), so the pressed summary moved up 48 px
  (top 756 -> 708). The release 290 ms later hit a SECTION below it, the click went to the common ancestor, nothing toggled.
- The 48 px are the line "Resolve the current reply before preparing access." (40 px + 8 px gap) in the Connected read review
  section: it shows while a turn is busy and leaves when the turn settles, which is some frames AFTER the reply text is visible
  (send() awaits several refetches before setBusy(false)). Measured locally with a per-frame probe: reply visible, +17 ms the
  suggested look and proposed terms appear, +49 ms that line leaves, -48 px. Locally this is over before the press; hosted it
  fell inside the 250 ms hold.
- That section is inside Details, ABOVE the conversation. Slice 2 made the driver call openDetails() before Send, so the
  journey ran with Details open, which is not the pane's default.
- NOT a product regression of slice 2: the reply's tool activity is part of the conversation, outside Details, and reachable
  with Details closed (the default). The same section and the same line sit above the conversation on main too; slice 2 only
  wrapped them in Details.
- Forced reproduction on the Mini (scratch, not committed: the last post-reply refetch is held by a route until
  page.mouse.down() returns, so busy -> idle lands in the hold): Details open: content -48 px, macOS Chromium's scroll anchoring
  moved scrollTop by -48 and the summary stayed (pass). Hosted Linux Chromium did not compensate (trace above). Why anchoring
  differs there, and why main passed hosted with the same line above the conversation, is NOT determined.
  Details closed (the fix): content height and scrollTop do not change at all (507 / 507), no reliance on anchoring, the
  disclosure opens; fractional and partial, Chromium.
- FIX, commit 377f9a820 `test(web): press the reply disclosure with Details closed` (driver only): generic-agent-chat.mjs no
  longer opens Details before Send; it presses the reply disclosure in the default state and calls openDetails() right before
  the first review step (proposed terms). It also uses recurringTurns(). No product change.
- Could another of the 10 ids fail the same way hosted? A held press (mouse.down ... mouse.up) exists in two other drivers:
  - scheduled-connected-read.mjs (late-activity and late-page cases) holds a press with Details open. Those presses come after
    terms, review form, schedule approval and a daemon turn, long after the interactive turn settled, and that line is not
    showing then; all 8 cases passed in the same hosted run. Residual risk: low, not zero (it relies on the same anchoring for
    the late page insert, which is what the test is for).
  - durable-composer.mjs holds a press before anything is sent, Details closed. Not exposed.
  - All other steps use Playwright click(), which waits for a stable position and presses and releases at once.
- Open product point, not changed here (same on main): with Details open, the busy line makes the sections above the
  conversation change height when a turn settles. Reserving that row would remove the shift for people who keep Details open.
- The browser lane ran SERIALLY (-n 0), as hosted does, on 377f9a820: 41 passed.

Sonar (it replaced fa74bea2..., head 334494c39, whose PR #789 was closed):
- The hosted Sonar gate failed on 334494c39: new_duplicated_lines_density 3.2% > 3% (12 lines in four journey drivers).
- Commit 7ba7da7b8 `refactor(web): one helper per repeated journey step, plain speaker label`, no behaviour change:
  - web/e2e/chat-ready.mjs gets `recurringTurns(page)` (the 4-line locator three drivers repeated) and
    `createHeinzelWithMessage(page, sentence)` (fill, create, wait, openDetails, assert the carried sentence, Send: the exact
    7 statements chat-only-workshop.mjs and template-scheduled.mjs repeated, same order). The drivers call them in one line.
  - DurableSession.tsx: the nested ternary of the row's speaker label is the function `speakerLabel` (same four outcomes).
  - web/src/heinzel/look.ts: optional fields use NonNullable<...> | null (type-only; no `| undefined` beside `?`).
  - Left as is: the `await` in the viewport loop of mobile-responsive.mjs (the sections must run in order) and S6819 role=status.
- Estimate with a rough stand-in for Sonar's detector (~/Code/tmp-web-wave-2/dup.py: runs of >= 10 identical trimmed lines
  shared by two files, counting lines added against main): 334494c39 8 lines in the two scheduled drivers, 7ba7da7b8 0 lines;
  with 6-line runs 20 -> 4. It is an approximation (Sonar matches tokens), not Sonar's number: the hosted gate decides.
- The same 20 files differ from main; the selection is unchanged.

History before that: this line of packets replaced 60dfdfd0... (head 3b2ebc6c0 on main 38c995170) and a23164c8... (head 3e1f1362).
- A local run of the journeys outside the selection found a real break on 3e1f1362: all six viewport sections of
  `tests/e2e/web_ui_test.py::test_mobile_touch_viewports_keep_navigation_settings_and_chat_reachable` timed out at
  `web/e2e/mobile-responsive.mjs:1105`, a click on "Customize your Heinzel · suggested look", which slice 2 (e5fd38b5b) moved
  behind the collapsed Details disclosure. The chain only repaired that driver later (f13e71056).
- Fix, commit 3b2ebc6c0 (3 lines, driver only, no product change): import `openDetails` and call it before that click.
- mobile-responsive.mjs is therefore a changed driver, and the head has 11 owning browser ids for a lane of 10. Builder decision,
  to be confirmed by the reviewer: the mobile node id is IN the hosted selection (it proved it catches a break) and
  `tests/e2e/connected_read_test.py` is OUT. Reason: its driver change is one line ("Queue message" -> "Send"), and the same
  connected-read review flow with the same button runs in the selected scheduled_connected_test (scheduled-connected-read.mjs,
  8 cases). It was run locally on this head instead (2 cases passed). Other one-line driver changes, if the reviewer prefers
  another id to leave: durable-composer.mjs (web_durable_composer, the only composer-reload journey), source-suggestion.mjs.
- Checked after #788: every web/e2e driver that touches the Heinzel pane either calls openDetails or passed on this head
  (connected-read, durable-composer, durable-five-minute, scheduled-notification-review, source-suggestion); no "Queue message"
  is left in web/e2e or tests/e2e.

Why the selection is declared, not derived:
- `hz-wave-size feat/web-wave-2` exits REFUSED: "e2e lane supervised has no focused lane mapping". Cause: `web/e2e/chat-ready.mjs`
  is a shared driver helper (new export `openDetails`) with no direct tests/e2e owner, so base affected.py selects all browser
  lanes including `supervised` ("no tests/e2e owner found, all browser lanes"). No supervised test imports chat-ready.mjs.
- web: `web/src/i18n.ts` (WEB_SHARED) changed, the selector says Vitest ALL -> `["web/ALL"]`, the whole suite (167 files, 1536
  cases, no duplicate (file, case) pair, every Vitest file of the tree has a case).
- python: the selector returns no pytest id (no Python production, no Python test changed). Declared 4 unit files that read the
  changed web files from disk: docs_style_test (copy in i18n.ts), branding_test (i18n.ts), standards_test,
  e2e_browser_output_test (scans web/e2e drivers). Builder choice; question it if you see another reader.
- browser (10/10): nine whole files that own changed drivers (chat_only_workshop, chat_profile_color, generic_agent_chat,
  read_set_approval_prototype, scheduled_connected, scheduled_notification_review, source_suggestion, template_scheduled,
  web_durable_composer) and the node id of the parametrized mobile test (10 sections), 41 cases. chat-ready.mjs is covered
  through its importers among them.

Outside the hosted browser selection, run locally on this head (5 cases passed, JUnit ~/Code/tmp-web-wave-2/r6/extra/junit.xml):
- `tests/e2e/connected_read_test.py` (owning, 11th id, see above).
- `tests/e2e/durable_five_minute_journey_test.py` (driver unchanged here; it becomes owning in the follow-up head).
- `tests/e2e/web_ui_test.py::test_mobile_sections_cover_every_viewport_of_the_driver` (reads the driver's viewport list).
The other 21 tests of web_ui_test.py and the rest of web-2 were not run on this head; the candidate runs the full lanes.

collect.err versus the manifest, web lane: on the earlier packets collect.err printed a web digest different from the
manifest's for the same pairs. Confirmed cause: collect_cases.py's informational `digest()` serialised with `ensure_ascii=False`,
while Core `focused_coverage.canonical` and hz_focused_manifest use json.dumps' default ASCII escaping; 40 web case names contain
non-ASCII characters, python and browser names are ASCII. The packet's value is always the manifest's (Core's formula), computed
from web.main-collected.xml; the collector's printed digest is not used anywhere. Compare the two lines in this packet's
collect.err and manifest: if they still differ for web, this is why.

Collector: this packet uses the shared default collector, which now maps `["web/ALL"]` to an unfiltered Vitest run (sha256 in
wave.json). Collection ran with HZ_ALLOW_FULL=1 because hz-gate refuses an unfiltered `vitest run` otherwise.

Local evidence is macOS with Vitest --maxWorkers=2 and pytest -n 2; hosted python/browser lanes are Linux, serial. Browser
cases include chromium and webkit params; confirm the case set is platform-independent.
No Python production changed -> mode python-coverage-not-applicable expected.
