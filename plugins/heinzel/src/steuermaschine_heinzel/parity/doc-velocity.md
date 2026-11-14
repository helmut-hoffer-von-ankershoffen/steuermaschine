# Parity: `sm doc velocity`, `sm doc velocity-record` against the former throughput tool

The measurement code and its 49 tests moved unchanged (`tests/coord_verbatim.py`: 25 names VERBATIM, `Api` without a
default runner; the test file identical below its fixture). The commands were compared with the old `main` by
reading: same option meanings, same refusals (one to three samples, positive integers, a valid repository slug, the
full head for a record), same text lines except the first line's prefix (`velocity:`), same report keys in JSON.
Differs on purpose: `--json` is the root option; `--cache` is the setting `velocity_cache_dir`; an unavailable forge
is exit 1 through the tool's refusal line. Not compared: a run against GitHub.
