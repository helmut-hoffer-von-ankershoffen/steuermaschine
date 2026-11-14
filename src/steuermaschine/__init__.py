"""steuermaschine: the artificial helmsman. Core package; everything project-specific lives in a plugin."""

PROG = "sm"  # the short console script; `steuermaschine` is the same program
API_VERSION = 1  # the plugin API; a plugin states the version it was written for
STATE_SCHEMA = 1  # the persisted run state
GO_SCHEMA = 1  # the `go --json` object

# Exit codes, the same for every command.
OK = 0  # finished, or stopped at a planned stop (review, dry run)
REFUSED = 1  # a gate or a step said no; the reason and the next command are printed
USAGE = 2  # wrong arguments or configuration
ALARM = 3  # an unacknowledged alarm holds
INTERNAL = 70  # a bug in the tool or a plugin
WAITING = 75  # nothing to do yet; run the same command again later
