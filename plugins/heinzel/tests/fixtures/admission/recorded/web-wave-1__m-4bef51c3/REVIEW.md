# preopen pair: feat/web-wave-1 head b0599126daff onto 4bef51c338eb (builder-prepared by hz-admit-wave; NOT the independent review)

Members: feat(system): MCP server switch (ADR 0146) + UI bundle 0912 + web bundle 0912b

- head `b0599126daff1592d913cc7795a189d0f87c228c`, base `4bef51c338ebdfbd788e1c06b3edd5de96f46aea` (given main), tree `4a605e3a2eae6f866109d94c8246125927d96afe` (git merge-tree)
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
head b0599126daff1592d913cc7795a189d0f87c228c
base 4bef51c338ebdfbd788e1c06b3edd5de96f46aea
tree 4a605e3a2eae6f866109d94c8246125927d96afe (merge-tree)
changed (47):
  M README.md
  M docs/CLI_REFERENCE.md
  M docs/CONFIGURATION.md
  M docs/MODULES.md
  M docs/adr/0062-heinzelmaennchen-find-the-mcp-servers-and-skills-you-gave-claude.md
  A docs/adr/0146-found-mcp-servers-are-on-by-default-and-each-can-be-switched-off.md
  M docs/adr/README.md
  M scripts/generate_docs.py
  M src/heinzel/modules/system/claude/__init__.py
  M src/heinzel/modules/system/claude/_models.py
  M src/heinzel/modules/system/claude/_render.py
  M src/heinzel/modules/system/claude/_scan.py
  M src/heinzel/modules/system/claude/_service.py
  A src/heinzel/modules/system/claude/_switch.py
  M src/heinzel/testing.py
  M tests/e2e/web_ui_test.py
  M tests/integration/claude_test.py
  A tests/unit/modules/claude_switch_test.py
  M tests/unit/modules/deferred_render_test.py
  M web/openapi.json
  M web/src/api/schema.d.ts
  M web/src/components/ContributionToggle.test.tsx
  M web/src/components/ContributionToggle.tsx
  M web/src/components/SettingsShortcut.test.tsx
  M web/src/components/SettingsShortcut.tsx
  M web/src/i18n.ts
  M web/src/layout/DaemonSettings.test.tsx
  M web/src/layout/FromClaudeSection.test.tsx
  M web/src/layout/FromClaudeSection.tsx
  M web/src/layout/Theme.test.tsx
  M web/src/layout/ThemePreviews.tsx
  M web/src/layout/TopBar.test.tsx
  M web/src/layout/TopBar.tsx
  M web/src/lifecycle/RestartAndApply.test.tsx
  M web/src/notifications/NotificationTest.test.tsx
  M web/src/notifications/TodayNotifications.test.tsx
  M web/src/notifications/TodayNotifications.tsx
  M web/src/notifications/notifications.test.tsx
  M web/src/onboarding/OnboardingWizard.test.tsx
  M web/src/onboarding/OnboardingWizard.tsx
  M web/src/pages/Contracts.tsx
  M web/src/pages/Heinzelmaennchen.test.tsx
  M web/src/pages/Heinzelmaennchen.tsx
  M web/src/pages/Profile.tsx
  M web/src/pages/Screens.test.tsx
  M web/src/test/copy-standards.test.ts
  M web/src/test/links-standards.test.ts
protected execution changed: none
pyproject.toml: unchanged
production (7): src/heinzel/modules/system/claude/__init__.py, src/heinzel/modules/system/claude/_models.py, src/heinzel/modules/system/claude/_render.py, src/heinzel/modules/system/claude/_scan.py, src/heinzel/modules/system/claude/_service.py, src/heinzel/modules/system/claude/_switch.py, src/heinzel/testing.py
repo selector (base scripts/affected.py): repository selector selects the full set; scripts/generate_docs.py: shared/sensitive path, selects the full set; src/heinzel/testing.py: shared/sensitive path, selects the full set; web/src/test/copy-standards.test.ts: shared/sensitive path, selects the full set; web/src/test/links-standards.test.ts: shared/sensitive path, selects the full set; advisory jobs commits, dependency-review, secrets, lint, setup, audit, test-linux, coverage, perf, e2e-cli, e2e-supervised, e2e-web-1, e2e-web-2, macos-smoke, macos-e2e, electron, template, core-wheel, consumers, build
web: web/ALL = the whole Vitest suite, no file filter (1 advisory id(s): ALL)
selection: DECLARED, delta vs repository selector:
  + browser tests/e2e/daemon_browser_proof_test.py
  + browser tests/e2e/doctor_notice_recovery_test.py
  + browser tests/e2e/onboarding_team_name_test.py
  + browser tests/e2e/web_ui_test.py::test_a_plugin_installed_outside_the_running_app_offers_restart_and_apply_in_the_footer
  + browser tests/e2e/web_ui_test.py::test_desktop_title_bar_row_in_mounted_chromium_and_unchanged_browser
  + browser tests/e2e/web_ui_test.py::test_hz_ui_opens_the_app_and_it_shows_the_doctor_report
  + browser tests/e2e/web_ui_test.py::test_mobile_touch_viewports_keep_navigation_settings_and_chat_reachable
  + browser tests/e2e/web_ui_test.py::test_phone_pages_never_pan_sideways
  + browser tests/e2e/web_ui_test.py::test_settings_search_font_floor_precedes_focus_for_phone_input_modes
  + browser tests/e2e/web_ui_test.py::test_theme_image_viewports_preserve_the_cap_hinge_in_actual_chromium
  + python tests/integration/api_input_schema_test.py
  + python tests/integration/claude_test.py
  + python tests/integration/cli_test.py
  + python tests/integration/discovery_test.py
  + python tests/integration/heads_test.py
  + python tests/integration/private_api_test.py
  + python tests/integration/startup_render_imports_test.py
  + python tests/unit/adr_index_test.py
  + python tests/unit/affected_test.py
  + python tests/unit/branding_test.py
  + python tests/unit/docs_style_test.py
  + python tests/unit/documentation_test.py
  + python tests/unit/heads/desktop_app_command_test.py
  + python tests/unit/heads/mcp_http_auth_test.py
  + python tests/unit/heads/mcp_standard_project_test.py
  + python tests/unit/kernel/kernel_test.py
  + python tests/unit/modules/claude_scan_test.py
  + python tests/unit/modules/claude_switch_test.py
  + python tests/unit/modules/deferred_render_test.py
  + python tests/unit/review_tier_test.py
  + python tests/unit/ship_test.py
  + python tests/unit/site_catalog_test.py
  + python tests/unit/source_review_test.py
  + python tests/unit/standards_test.py
  + python tests/unit/testing_test.py
  + web web/ALL
  (no bounded derivation)
case_sha256 browser 63a902245a85e9331ebc576df6a0425109b8ea897fdd8a958dbf3abc89c13755 (junit sha256 45033e31966d0afa945584b6bfd0e4cfe44e38a2f11f4ced0f510100614c1a7e, 24 cases, NOT bound to head by Git: confirm its run)
case_sha256 python 8b1d0e1ddb20cc15226dbda7313c6880916d85868c399bad8f642577d0b6ac29 (junit sha256 25d5f175ad5b07e1408161cf4f495d12bcc0b9f2322b84bcca10ca841f43121b, 3082 cases, NOT bound to head by Git: confirm its run)
case_sha256 web 28cf243137c98500435e235ae1887fe462b0b0670531d834e3e1af3a3a309c3d (junit sha256 b14b3c75e49b4c72afdd5e30f510b8bda58c1c4186122695c0102b36eba4d485, 1531 cases, NOT bound to head by Git: confirm its run)
manifest_sha256 dd6eb0fa5307b332b15d30e927a911612facb73ac3408dc6d7e5cb45bd21458d
main_manifest_sha256 f144a3f9d9879d491c306c488b66c48fa15f6ddc88fe7cb90f803a1d66647ce6
review: none yet (not admissible; review these digests, then rerun with --review)
wrote /Users/<user>/Code/tmp-pr-fill/admit-B/web-wave-1/m-4bef51c3/packet.json sha256 ea1b5df3b44cf229d619b33b2067eafb54e71efc73a2445fd0a93f0fe950726f
```

## Notes for the independent reviewer
## Builder notes on the declared selection (not the independent review)

Head: `feat/web-wave-1` b0599126d = main + three reviewed local branches as normal merge commits (no squash, no rebase):
fix/ui-bundle-0912 (e2699f763), feat/web-bundle-0912b (9a7f1f855), feat/mcp-server-switch (6dc1f299b), then
origin/fix/personal-artifacts-test-names (#781), main 38c995170 and main 4bef51c33 (#785 lifecycle wait, #786 price
snapshot; no conflict, no file of this head's diff touched), plus two builder commits: 69c8c180a (test fixture isolation,
below) and b0599126d (one test renamed, below). All three JUnit files and the scoped coverage were produced on b0599126d.

Lint (PR #787 on 69c8c180a failed hosted `make lint`, step typos: `entrys` in a test name of
tests/unit/modules/claude_switch_test.py). b0599126d renames the case to
`test_switching_one_on_keeps_another_off_that_only_the_tombstone_of_a_gone_entry_covered`; body unchanged. On b0599126d:
`hz-gate make lint` exit 0, all 16 steps green (lock, format, ruff, contracts, template, docs, design, demo, attributions,
zizmor, actionlint, typos, docs-check, confidential, web-lint, web-client). `uv run typos` (typos-cli 1.51.1) over every
file changed against main, and over the added lines alone: exit 0.

Scope: the title is feat(system), routed as mixed/system (not combine:web). This head carries Python production: the core MCP server switch
(src/heinzel/modules/system/claude/*, ADR 0146, private operations for CLI and web, the switch file and HZ_CLAUDE_SWITCH_DIR).
Review it as production code; the UI bundle rides along.

Why the selection is declared, not derived (`hz-wave-size` exits REFUSED, "repository selector selects the full set"):
- scripts/generate_docs.py: shared path, selects the full set. The change is six lines of CONFIGURATION.md prose about
  HZ_CLAUDE_SWITCH_DIR, no generator logic.
- src/heinzel/testing.py: shared path, selects the full set. The change is one line: HZ_CLAUDE_SWITCH_DIR joins the
  isolated-environment names.
- web/src/test/copy-standards.test.ts and links-standards.test.ts: shared web paths (allowlist entries removed). With
  web/src/i18n.ts they make the selector say Vitest ALL.
- web: `["web/ALL"]`, the whole suite: 167 files, 1531 cases, no duplicate (file, case) pair, none skipped.
- python (25/32 files, 3082 cases, none skipped; two more than on the previous head, which arrived with the merge of main), by closure, builder choice; question it if you see another owner:
  - MCP switch (the branch's recorded files): unit/modules/claude_switch_test, integration/claude_test; plus
    unit/modules/claude_scan_test, unit/modules/deferred_render_test, integration/startup_render_imports_test,
    integration/discovery_test, unit/heads/mcp_http_auth_test, unit/heads/mcp_standard_project_test.
  - new operations on the heads: integration/private_api_test, api_input_schema_test, heads_test, cli_test.
  - testing.py: unit/testing_test, unit/kernel/kernel_test.
  - generate_docs.py and generated docs: unit/documentation_test, adr_index_test, docs_style_test, standards_test,
    site_catalog_test, ship_test, review_tier_test, unit/heads/desktop_app_command_test (CLI reference).
  - readers of web files from disk: unit/branding_test, docs_style_test, affected_test, source_review_test.
  - NOT in the lane: the other importers of heinzel.testing (conftest and about a dozen credential, notify, daemon and
    setup tests). The one added environment name does not change their behaviour; that is the residual gap.
- browser (10/10 ids, 24 cases, chromium and webkit params): tests/e2e/web_ui_test.py journeys doctor report (ui.mjs:
  contracts, Heinzel rows, setup), mobile touch viewports (all sections), restart-and-apply footer (lifecycle copy), theme
  viewports, phone pages never pan, desktop title bar row (TopBar), settings search font; plus the files
  onboarding_team_name_test (setup wizard), daemon_browser_proof_test (restart copy), doctor_notice_recovery_test
  (notices). No web/e2e driver changed in this head. Left out: the rest of web_ui_test.py (reopen, reconnect, gallery,
  calendar, sidebar, sound picker, media layers), daemon_install_ui_test and all chat journeys; none of their drivers or
  owning components changed, but any of them can read changed copy from i18n.ts. No browser journey exercises the
  From Claude switch; it has Vitest cases (FromClaudeSection.test.tsx) and the screenshots only.

## Scoped coverage (hosted computation reproduced locally)

`hz-gate .venv/bin/python ~/Code/tmp-pr-fill/admit-B/scoped_cov.py ~/Code/heinzel-web-wave-1 inputs/sel.json` on b0599126d
(`hz-scoped-cov`, same script), macOS, serial, the 25 selected python files; denominator = every statement and branch arc of the 7 whole changed production files:

```
src/heinzel/modules/system/claude/__init__.py: 100.00% (40/40) missing lines [] missing branches []
src/heinzel/modules/system/claude/_models.py: 100.00% (65/65) missing lines [] missing branches []
src/heinzel/modules/system/claude/_render.py: 83.58% (56/67) missing lines [29, 61, 71, 72, 73, 88] missing branches [[28, 29], [30, 32], [32, 50], [50, 60], [60, 61]]
src/heinzel/modules/system/claude/_scan.py: 88.50% (377/426) missing lines [70, 71, 75, 76, 189, 251, 252, 253, 254, 262, 287, 302, 308, 309, 323, 324, 332, 333, 349, 350, 370, 371, 373, 413, 414, 423, 424, 426, 456, 464, 465, 480, 483] missing branches [[74, 75], [171, 170], [186, 189], [252, 253], [252, 254], [261, 262], [286, 287], [301, 302], [316, 310], [329, 337], [372, 373], [425, 426], [453, 451], [455, 456], [479, 480], [482, 483]]
src/heinzel/modules/system/claude/_service.py: 100.00% (98/98) missing lines [] missing branches []
src/heinzel/modules/system/claude/_switch.py: 92.20% (130/141) missing lines [129, 130, 132, 155, 156, 157, 193, 194, 195, 196] missing branches [[131, 132]]
src/heinzel/testing.py: 92.35% (157/170) missing lines [78, 181, 185, 189, 193, 255, 257, 258, 259, 269, 271, 272, 273] missing branches []
SCOPED 91.658% (923/1007) threshold 90
```

91.658% is above the 90% report threshold and the 80% floor, by 17 of 1007 units. No test was added. The uncovered parts are
mostly pre-existing code of _scan.py (error and platform paths) and testing.py, and the render paths of _render.py. The
hosted lane is Linux: platform branches can move the number by a few units; 7 python slots stay free (25/32) if it falls
short there.

## e2e isolation of the switch file (review finding 2)

- Fixed in 69c8c180a: the `environment` fixture of tests/e2e/web_ui_test.py drops HZ_* and keeps the real HOME, so the
  started web head would resolve the real home's claude/servers.json. It now sets HZ_CLAUDE_SWITCH_DIR to a tmp dir.
  tests/e2e/ai_access_install_ownership_test.py imports the same fixture. The neutral gallery env in the same file sets
  its own HOME and needs nothing. The 10 browser ids were rerun on b0599126d: 24 passed.
- Checked, already isolated (own HOME): ai_access_install_ownership_test (second env), offline_uninstall_test,
  integration/ui_service_test, tests/e2e/_ui_service.py. In-process journeys get HZ_CLAUDE_SWITCH_DIR from hz_isolation.
- ACCEPTED GAP, bead heinzel-management-p4nx (P2): tests/e2e/auto_update_test.py and web_durable_composer_test.py build
  their env the same way (drop HZ_*, real HOME) and are NOT changed here: they are browser files outside the full 10/10
  lane. Only Settings > From Claude calls system.claude; neither driver (auto-update.mjs, durable-composer.mjs) opens it,
  and reading the file writes nothing; a write needs a click on a switch or on the notice.
- No browser journey clicks the switch: bead heinzel-management-l7mb (P2).

Hand-made merge resolutions, not seen by any branch reviewer:
- tests/unit/modules/deferred_render_test.py: `assert len(checked) == 52`. Main had 50 (ADR 0145 renderers), the MCP branch
  48 from a base of 46 (+ render_switch, render_closed). 50 + 2 = 52; the test passes with exactly that count.
- tests/unit/heads/desktop_app_command_test.py: main's version kept. The bundle's CLI commit 2fd724a58 is on main as #779
  with more cases; it stays in history and adds no diff.
- docs/adr/README.md: regenerated by scripts/generate_docs.py (rows 0145 and 0146), not hand-merged.
  `generate_docs.py --check` and `web.py client --check` exit 0 on the head.

Beads, all partial (none closes with this head):
- hhz: settings shortcut is a ghost pane action; contract, Heinzel and notice rows are navigation rows. Remaining: the
  other footer panes' structure.
- eu8z: setup and profile open Settings with buttons; rows are no longer text links. Remaining: the Calendar pane link,
  the rule and the full sweep.
- 7feb: daemon, notification, restart and operation-failure copy shortened; a switch shows Details only when they add
  information (core half). Remaining: the Tailscale plugin half and the other panes.
- 8n9 (epic): theme name stays whole on narrow preview cards. Remaining: the audit of all other screens.
- qnv: the refresh button says why it spins. Remaining: verification on the Mini, button position, sidebar browser.
- MCP switch (ADR 0146, implementation: partial): servers are listed and switched; nothing starts or uses them yet.

Checked on main: the mascot overlapping the "Who you are" heading in the setup dialog is the same on main 38c995170
(same isolated setup, same viewport); it is not introduced here.

Local evidence: macOS (Mini), hz-gate; Vitest --maxWorkers=2; pytest -n 2 for python, serial for browser. Hosted python
and browser lanes are Linux; confirm the case sets are platform-independent. tsc and biome ci exit 0.
Not verified: hosted CI, macOS jobs, the desktop tests, the scoped coverage as the hosted Linux lane computes it.
