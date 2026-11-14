# Retired, not ported: the popup guard (`hz-popup-guard`, 13 lines sh)

Decision: **retired** (owner decision, 2026-11-14). No command, no tool-map path; the tool-map row is kind retired. The port
`sm daemon popup-guard` (native/popup.py, its launchd template and tests) was removed unshipped after its review found it
ends two system processes every second whether or not a test runs.

What it was: a `while` loop that runs `pkill -x "Problem Reporter"` and `pkill -x CoreServicesUIAgent` once a second until the file
`~/.hz-popup-guard.off` exists. Written 2026-11-07 as a temporary guard against the macOS "damaged" dialog that the desktop
installation test provoked by executing a copied platform binary.

Evidence (all read only):
- it is not in any launchd plist (`~/Library/LaunchAgents`: only the leak sampler and load guard plists mention a guard), not in the Claude hook
  settings (`~/Code/.claude/settings.json` registers the git hook only; `~/.claude/settings.json` has no hooks), and no other tool starts it;
  the only readers are the digest (`popup guard on|off` from the existence of the `.off` file) and notes;
- a copy of the loop **is running right now**, started by hand (a `/bin/sh` process on the tool, found with `pgrep`; `~/.hz-popup-guard.off` does not
  exist). It was not touched;
- the cause is closed locally: the heavy-run hook (`sm guard heavy`) refuses both desktop tests everywhere, even through the run lock ("hosted CI runs
  them"), so the dialog cannot be provoked from an agent shell any more;
- killing two macOS system UI processes in a loop is a side effect on the owner's desktop that a port should not turn into a permanent command.

Follow-ups for the owner or the lead (not done here): stop the running loop with `touch ~/.hz-popup-guard.off`; drop the `popup guard on|off`
item from the digest. If the dialog ever returns, add `sm daemon popup-guard` as a tick with its own review.
