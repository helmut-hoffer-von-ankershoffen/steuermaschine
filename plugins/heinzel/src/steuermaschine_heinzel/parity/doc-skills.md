# Parity: `sm doc skills generate | check | link` against the former skills tool

Not a port of the generator: the old one read each script's `--help` and a mapping file; this one reads the command
tree and derives a skill's commands from what the skill names. Kept, with the old tests' assertions: front matter,
name equals directory, a description that says when, at most 150 lines, a stale generated file, the link rules
(`frontmatter` is the old function, `tests/coord_verbatim.py`). New checks: a named command that does not exist, an
old tool name (with the command it became, from the tool map), a command no skill names.
The help lint: one implementation, the core's `sm self lint` on the built command tree (tool map row). The old
lint parsed script sources; its letters P W C E N are the core's `helpdoc.missing`; its letter S (asking for help
cannot act) holds by construction; its ranking of scripts has no successor.
