# Parity: `sm beads agents add|list` against the former agent table tool

The old test moved with its assertions (`test_agent_add`); `add` is the original function with the file as a
required keyword. `list` prints the lines that start with `| `, as before. Differs on purpose: the table's path is a
setting; a missing table is exit 2.
