# Parity: `sm beads stamp`, `sm beads stamp-released` against the former stamp tool

Compared: the old tool's six unit tests (earliest tag, line and label helpers, unmerged refusal writes nothing,
idempotent merged stamp, unpublished release refused, sweep stamps only merged and unreleased beads), moved with
their assertions to `tests/test_native_coord_journal.py` on the fake process runner. The five pure functions are
byte-identical (`tests/coord_verbatim.py`). Side effects and their order: reviews/beads-stamp.md and
reviews/beads-stamp-released.md. Differs on purpose: listed there.
Not compared: output against the real tracker and real GitHub (no write was made during the port).
