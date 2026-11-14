# Switch: state files move from ~/.cache/hz-* to the project's state directory

From this release on, the plugin keeps its state in the project's state directory (`sm self config` shows it as
`state`, on the Mini ~/.local/state/steuermaschine/heinzel/), under plain names: `queue.json`, `train/`,
`release.json`, `gate.lock`, `alarm.json` and so on. The registry is `plugins/heinzel/src/steuermaschine_heinzel/state.py`.

## How the move happens

- `sm self migrate-state` moves every old path in one pass (`--check` lists what is still old, `--list` lists every
  move). A file gets a hard link at its new name and the old name becomes a symlink to it in one rename (same
  inode: a lock held across the move still excludes); a directory is renamed and the old name links to it. One
  problem (both names hold state, another file system, a link that points elsewhere) refuses the whole pass.
  Every rename is logged on stderr and in `state-migrations.log` in the state directory.
- bin/promote runs it (`migrate_state_to` in bin/promote_lib.sh) after `wait_idle` and before the switch; a
  refusal stops the switch. bin/rollback moves back what only the newer release knows (`--back --keep`) before
  it switches. Nothing moves at run time: code of this release refuses while state is still at an old name.
- Every write command holds `writers.lock` (shared) in the state directory while it runs; `migrate-state` takes it
  exclusively, so no writer of this release or a later one runs during a move.
- bin/migrate_config.py rewrites a config value that names an old state path to its new name (key names only are
  printed).

## The first promote with this release (one time, by hand)

The release that is live before this one has no writers' lock: a loop tick of the old code that started just
before the switch-pending file could write through a symlink during the move. For this one promote the integrator
stops the loops first (the plists stay as they are):

1. `launchctl bootout gui/$(id -u)/io.github.helmut-hoffer-von-ankershoffen.hz-queue` (and `.hz-train` if it is
   loaded, `.hz-leakwatch`, `.hz-loadguard`).
2. `bin/promote` (it runs `migrate-state` while idle, then switches).
3. `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist` for each loop stopped in step 1.

After that, `sm self migrate-state --check` exits 0. Later promotes need no manual step.

## Outside sm

The old paths stay readable through their symlinks. The documents that still name an old path (the tracking
document, the release skill's rules, the train timer note) are updated by the coordinator; the launchd plists keep
their own stdout logs in ~/Library/Logs.
