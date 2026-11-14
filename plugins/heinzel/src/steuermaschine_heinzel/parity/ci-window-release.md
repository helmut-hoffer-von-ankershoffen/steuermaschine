# Parity: `sm ci window release`

New command, no old counterpart: the former per-branch lock file died with its process, the window's state does not.
It ends a turn whose command is gone (or any turn with `--force`) and removes a leftover validation/* branch when no
run is active on it. Compared with nothing; tested on fakes (`test_native_dispatch.py`, `tests/test_core_window.py`).
