# head cases collect | compare: parity with the former case collector

Code: `native/packet_lib/collect_cases.py` is the former collector; `packet_verbatim.py diff --only collect_cases`
prints the one changed hunk (the defaults of `--python`, `--node-modules`, `--work` no longer name directories of the
build machine). `packet_lib/check_ops.py` starts it for `collect` and calls its `compare` in process.

Compared, 2026-11-12:
- live, `sm head cases collect` with the Core checkout's interpreter on the recorded resulting tree 2644da58 of
  e2e-details/m-437bb247: python lane (2010 cases, digest ff4e5b2c…) and browser lane (42 cases, digest 3c8dffd1…):
  the written JUnit files are byte-identical to the recorded `*.main-collected.xml`; the stderr line is the former
  collector's;
- `sm head cases compare` on recorded files: identical, exit 0, as recorded (empty case-diff files);
- in tests: the digest of every recorded inventory equals the case digest its packet binds
  (`test_recorded_inventories_give_the_recorded_case_digests`), and the exit codes 0, 1, 3 with their id lines.

Not compared live: the web lane (it runs Vitest, the whole suite for `web/ALL`); in tests a stand-in vitest shows the
file filter and the sentinel.

Different on purpose: two commands instead of two sub-operations of a script; `collect` runs under the run lock and
prints the result (lane, tree, count, digest, file) on stdout, json as `CasesCollected`; `compare` prints its verdict
line before the ids, json as `CasesCompared`; a failing collection is a refusal (exit 1) through the core.
