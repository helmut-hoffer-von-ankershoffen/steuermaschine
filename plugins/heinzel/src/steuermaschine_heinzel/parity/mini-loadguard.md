# mini loadguard (read-only)

The old guard's `status`: `paused PID for Ns: COMMAND` per paused tree, or `nothing paused`. Compared live on
2026-11-12 23:40 (`nothing paused`). The guard itself (`tick`, run by launchd, which pauses and continues process
trees) is not ported here and keeps running as it is; this command reads the state file it writes
(setting `loadguard_state_file`; no default, the command refuses until it is set). The JSON document adds the load and the two thresholds.
