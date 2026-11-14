# Parity: `sm note` against the former note tool

Compared: the old tool's two tests (one line with squeezed spaces, second line after it; no text is a usage error),
moved with their assertions to `tests/test_native_coord_journal.py`. Line format `- MM-DD HH:MM text`, stdout `noted`.
Side effect: one append under the same lock file. Differs on purpose: see reviews/note.md (final newline guard, exit 2).
Not compared: a run against the real document (tests work on sandbox files only).
