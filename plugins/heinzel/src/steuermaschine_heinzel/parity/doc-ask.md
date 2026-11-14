# Parity: `sm doc ask`, `sm doc asks`, `sm doc asks-ack` against the former ask tools

Compared, with the old tests' assertions (`tests/test_native_coord_asks.py`): a row is added at the end of the asks
table and is open until it names a pull request; the transcript extraction on the old fixture (four owner messages
out of eleven records); the audit's MSG lines, truncation at 160, the count line, exit 1 then 0 after the
acknowledgement; a message arriving after the listing stays unreviewed; the newest top-level transcript is the default.
Moved functions are identical (`tests/coord_verbatim.py`: STAMP, PR, CONTEXT_BLOCK, owner_text, owner_messages,
short_time, title).
Differs on purpose: `--check`, `--audit` and the ingest listing are one read-only command `sm doc asks`; below each
message it prints ONE command (`sm beads tasks add TITLE --ask`) instead of two; `--ack` takes the timestamp; the
watermark lives in this tool's state and the former file is read only (reviews/doc-asks-ack.md).
Not compared: a run on the real transcript and the real document.
