# head size: parity with the former wave sizing

Code: `native/packet_lib/wave_size.py` is the former script minus its `--help` shortcut and its argv entry
(`packet_verbatim.py diff --only wave_size` prints those hunks). `ops.size_wave` runs the same steps as its `run`.

Compared, 2026-11-12: the three recorded heads (e9c94381, 7dc85604, 5e409527) at main 437bb247, old tool and
`sm head size`, same repository: stdout byte-identical, both exit 1 (two conflicts, MOVE 7dc85604). In tests:
`test_size_of_a_real_branch_in_text_and_json_equals_the_moved_report` and the conflict test compare the lines of the
moved `run` with the command's output.

Different on purpose: no `$HZ_CORE` (setting `core_repo_dir`, or `--core`); unreadable state is a refusal with exit 1,
not exit 2; `--format json` gives the facts behind the lines.
