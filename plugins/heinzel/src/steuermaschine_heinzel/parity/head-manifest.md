# head manifest: parity with the former manifest builder

Code: `native/packet_lib/focused_manifest.py` is the former module, moved without a changed line
(`python plugins/heinzel/tests/packet_verbatim.py diff --only focused_manifest` prints VERBATIM). The native command
calls its `build`, `packet_bytes`, `read_junit` and `read_review` from `packet_lib/ops.py`.

Compared, 2026-11-12, with `python plugins/heinzel/tests/packet_parity.py --core GIT` (and `test_native_packet_parity.py`), on
the four packets the former tools built that day (fixtures/packet/recorded, copied unchanged), against a local
repository that still holds their commits:

| Packet | sha256 of packet.json (recorded = rebuilt) | verify |
|---|---|---|
| e2e-details/m-437bb247 (preopen, admitted) | d846d7c9f8dbb6518737b2d15b90a17d6ed1cbb5131d97947d705c10b17e2f99 | byte-equal |
| e2e-details-2/m-437bb247 (preopen, admitted) | a1e454ecec2fe9131557a455e2c417db3a0000f46ba116a0e023b212ee90f2a5 | byte-equal |
| e2e-daemon-race/predict (predicted base) | e0848a7ee75c6c5563f5a0058b267cc087936ea481e9b53c10c0452460173679 | byte-equal |
| e2e-daemon-race/predict/bound-f19a3782232c (bound) | 4e17be61fc5cd5511cc00f66c417f76eaa43eba14ef3c01ef57a78279078450d | byte-equal |

For each: `rebuild` builds from the recorded inputs (sel.json, the collected JUnit files, the recorded REVIEW.md as the
bound review) and compares the packet bytes and the report with the recorded `manifest.final.err` (the output path in
its last line replaced); `verify` regenerates the recorded packet and compares the report with the recorded
`verify.err`. All eight: identical.

Different on purpose:
- `--format indent|canonical|canonical-nl` is `--layout` (the root option `--format` is text or json).
- A build needs `--output`; the packet is not printed to stdout (stdout carries the result).
- A PR number as TARGET is read through the one GitHub client, then handled as its head commit.
- The default review roots are gone: `--review` needs `--review-root`; `--verify` looks beside the packet.
- A refusal is one line through the core (exit 1); usage errors exit 2. The report lines are the result's `summary`
  and outcome line (text: stdout; json: fields), not stderr.
