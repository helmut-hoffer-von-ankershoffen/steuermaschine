# Retired: the stale-loop finder (`hz-reload-loops`, `hz_reload_loops.py`)

Not ported. It compared the start time of four launchd loops with the modification time of the script files they
import, and restarted a loop that ran older code. Both halves are gone with the scripts:

- a native loop is `sm daemon NAME --loop SECONDS`, and the core ends such a loop by itself when the installed release
  changes (`steuermaschine.native`, "a daemon loop ends when the release changes"); the service manager starts it
  again on the new code. There is no stale loop to find;
- there are no script files left whose modification time could be newer than a loop.

Callers that still name it (to be changed with the switch of the loops to `sm daemon ...`, not before):
- the old queue loop and the old tick digest call it as a process (they are retired with their tools);
- the skills `hz-mini` (step 4), `hz-ci` and `hz-release` (references) say "after a fix to a tool a launchd loop runs:
  `sm mini loops`": replace with "nothing to do: a daemon loop restarts itself on the new release";
- no launchd job and no Claude hook calls it (checked: `~/Library/LaunchAgents`, `~/Code/.claude/settings.json`).

Until the last old loop is switched, the old script stays where it is and keeps working for the old loops.
