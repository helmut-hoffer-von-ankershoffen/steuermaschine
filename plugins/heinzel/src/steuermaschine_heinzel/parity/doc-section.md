# Parity: `sm doc section` against the former section reader

The old tool had no test. `headings` and `find_sections` are the original functions, now in the core
(`src/steuermaschine/docsection.py`, checked by `tests/coord_verbatim.py`); `read` is the old `main` body returning
its lines. Compared on the real tracking document (read-only) on 2026-11-12, old tool and new command: `--toc`, `--toc --level 2`,
`--sec 4`, `--sec 8 --max-lines 30`, `--grep 0.9.40 -C 1 -m 3`: identical output (after removing the old tool's name
from the cut marker). Differs on purpose: the cut marker no longer names
the old tool; FILE defaults to the setting `coord_document`; a relative FILE is also looked up next to that document;
`-C` and `-m` have long names too; an unreadable file or a bad regular expression is exit 2 with one line.
