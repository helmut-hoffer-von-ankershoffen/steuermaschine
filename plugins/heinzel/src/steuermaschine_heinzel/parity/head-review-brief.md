# Parity: `sm head review-brief` against the former review-brief tool

The brief text is the old one except its second line, which named the old guard tool and now says "The diff guard".
`members` is identical (`tests/coord_verbatim.py`). Differs on purpose: the old tool ran the diff guard itself; this
command takes the guard's output with `--flags FILE` (`-` for standard input), because the guard is not native yet
and no command may start an old script. When `sm head guard` exists, it should call `briefs_lib.reviewer.build(pr,
flagged_text)` or this command should call it; one line either way. Verdict files are looked up in
`<coord_tracker_dir>/research/`. No old test existed for this tool; three new tests cover it.
