# Parity: `sm beads sweep` against the former bead sweep

Compared, with the old tests' assertions (`tests/test_native_coord_sweep.py`): matching by noted number, bead id and
three title keywords; hints A? to D?; the stale filter; the summary line; proposals only for class A with released
evidence at or below the verified version; a malformed or keyword-only release never supports a proposal; the
inventory and the summary never start a live verification. The eleven moved functions are the original code
(`tests/coord_verbatim.py`); three have a meant difference, printed there.
Differs on purpose:
- The evidence document is `--format json` (one document of the model `Sweep`); the option `--json FILE` and the
  file `last.json` are gone. The pull request list is kept for ten minutes in this tool's state, not in a cache file.
- `--propose-close` asks the native live verification (`mini.verify`) in process instead of parsing the old
  verifier's output. On a tree without that command it never proposes (exit 1). The old tests of the output parsing
  (eleven shapes of verifier output) became: the verdict must be live and name exactly the inventory version.
- The close reason names `sm mini verify`.
Not compared: a run against the real tracker and GitHub.
