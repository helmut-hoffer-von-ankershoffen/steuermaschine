# head packet preopen | predict | bind | wave: parity with the former wave builder

Code: `native/packet_lib/admit_wave.py` is the former builder module and `collect_cases.py` the former case collector,
moved; `python plugins/heinzel/tests/packet_verbatim.py diff --only admit_wave --only collect_cases` prints every
changed line. `packet_lib/wave_ops.py` gives its four operations typed arguments and results.

## Compared, 2026-11-12 (`python plugins/heinzel/tests/packet_parity.py --core GIT`, also `test_native_packet_parity.py`)

Recorded packet directories of the former builder, copied unchanged to tests/fixtures/packet/recorded; the Git
objects come from a local repository that still holds the recorded commits, used through a `git clone --shared` in a
temporary directory (nothing is written to it).

| Check | Recorded directory | Result |
|---|---|---|
| preopen | e2e-details/m-437bb247 (packet d846d7c9…, admitted) | identical |
| preopen | e2e-details-2/m-437bb247 (packet a1e454ec…, admitted) | identical |
| predict | e2e-daemon-race/predict (packet e0848a7e…; predicted main da136230…) | identical, same predicted commit |
| bind | e2e-daemon-race/predict/bound-f19a3782232c (packet 4e17be61…) | identical |

"Identical" is: sel.json, collected.txt, the case-diff files, the collected JUnit files, manifest.err,
manifest.final.err, verify.err, REVIEW.md, packet.json and wave.json equal the recorded files, byte for byte, with
exactly these allowances, each forced by rebuilding under another directory:
- the absolute review root: it is quoted in the reports and in REVIEW.md ("wrote ROOT/…/packet.json");
- therefore the sha256 of REVIEW.md, which packet.json binds (`review.sha256`), and the sha256 of packet.json quoted
  in the two final reports and in a bound REVIEW.md. The check replaces the root in the rebuilt REVIEW.md and
  requires the recorded review digest; it replaces that one digest in the rebuilt packet.json and requires the
  recorded packet bytes and sha256. `head manifest` given the recorded REVIEW.md gives the recorded packet.json with
  no allowance (parity/head-manifest.md);
- wave.json: the machine paths, the collector's path and digest, the collection python.
The builder label in the REVIEW.md title is taken from the recorded file for these runs (see below).

Collection: in the checks above the collection run is replayed from the recorded collected JUnit files. Live, the
moved collector under `sm gate run`, with the Core checkout's venv, for e2e-details/m-437bb247: the four collected
files (python and browser lane, head tree and resulting tree; 2010 and 42 cases) are byte-identical to the recorded
ones.

## Different on purpose

- The builder's REVIEW.md title says "builder-prepared by sm head packet".
- The command a build names next is `sm head prepare …` (preopen, bind), not a script path.
- wave: the checks are unchanged, but it names no command. The batch form of `head prepare` (`--packets`,
  `--core-repos`) is not native yet; the result lists the packets and checkouts in batch order and says so
  (`batch_prepare_ported: false`).
- The manifest is built and verified in process (`ops.build_packet`, `ops.verify_packet`); the reports it writes to
  manifest.err, manifest.final.err and verify.err are the same text.
- The collector is the package's own file, run as before as a script in its own isolated interpreter (`python -I`);
  it is the one `__main__` of this area, listed in tests/gate_known.json with the reason it cannot write. Each
  collection run takes the run lock (`steuermaschine.runlock`, the policy of `sm gate run`) instead of a wrapper
  command. Its `--work` default is a temporary directory; the builder passes a fresh one and removes it.
- stdout is the result: text prints the packet, its sha256, the collection lines, the review path and the next
  command; `--format json` is one `PacketBuilt` (wave: `WavePlan`) document. The former start/outcome event lines
  are gone. A refusal is one line through the core, exit 1.
- predict refuses the repository named by the setting `core_repo_dir` (it writes objects; use a scratch clone).
- A `--selection` lane with an empty list is dropped (2026-11-13, heinzel-management-igag): `"python": []` on a
  web-only head means no python lane; the former builder refused it. A selection with no non-empty lane still refuses.

## The documented exception to "no old tool name"

The author and committer identity and the message of a predicted commit, and the scratch ref prefix it is stored
under, keep their historic bytes, which contain the former builder's name. They are hashed Git data: they decide the
SHA of every predicted commit, recorded predict packets bind that SHA as their base, and `bind` and `preopen`
recognise a predicted commit by exactly these values. Changing them would change every predicted commit and make
existing predict directories unbindable. They appear in Git objects and in wave.json values, never in help, a
result model's field names or a message of a command.
