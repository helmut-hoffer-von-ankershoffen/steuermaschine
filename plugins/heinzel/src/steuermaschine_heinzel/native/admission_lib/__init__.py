"""Focused admission: the moved logic behind `sm head prepare|apply|blockers|hash`, `sm release admit` and the packet builders.

The focused_* modules were moved from the former tools with their tests (tests/admission_verbatim.py shows every
line that differs). `ops.py` is the typed entry the native commands call; `proc.py` is the one process boundary.
"""
