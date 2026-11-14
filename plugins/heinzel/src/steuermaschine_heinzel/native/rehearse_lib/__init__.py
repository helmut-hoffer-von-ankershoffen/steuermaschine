"""The release rehearsal: the moved logic behind `sm release rehearse ...` and `sm daemon rehearse`.

`rehearsal.py` is the former rehearsal tool with its edges replaced; `others.py` holds its two guards that look at
other areas (the merge queue, a running admission); `legacy.py` reads the merge queue's state file, read-only.
"""
