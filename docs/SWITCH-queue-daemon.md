# Moving the queue service to the `sm-queue` launchd label

For the coordinator. The queue loop already is `sm daemon queue --loop 1`; on a machine that set it up before this
page it may still run under an older label. This page moves it to the label of the repository's template. Nothing
here was run against launchd by the author; every command is for you to run.

## 0. What depends on the label

`sm head apply` and `sm release admit apply` pause the queue service around their registry write: they boot it out,
prove it gone, write, and bootstrap it again from `~/Library/LaunchAgents/<label>.plist`. The label comes from the
setting `queue_service_label` (`sm self config | grep queue_service_label`). Unset, both refuse before they pause
anything and name the key. Set to a label that is not loaded, they refuse with "queue must be running before
controlled pause". So the setting and the loaded job must change in the same step.

| | Before | After |
|---|---|---|
| launchd label | the label in `queue_service_label` today | `io.github.helmut-hoffer-von-ankershoffen.sm-queue` |
| plist | `~/Library/LaunchAgents/<old label>.plist` | `plugins/heinzel/launchd/<new label>.plist` of the live release, copied there |
| program | `sm daemon queue --loop 1 ...` | the same |
| state, lock, log | the files `sm self config` names (`queue_state_file`; lock and log beside it) | the same |

## 1. Before (reads only)

```sh
sm self config | grep -E 'queue_service_label|queue_state_file'   # note both values
launchctl list | grep queue                                          # the loaded label must equal queue_service_label
sm head status; sm release status                                    # no admission or release step may run during the move
sm queue why                                                         # best moment: "queued: none"
```

## 2. The move (about one minute; never between `batch:` and `tick finished` in the queue log)

```sh
uid=$(id -u)
OLD=<the value of queue_service_label from step 1>
NEW=io.github.helmut-hoffer-von-ankershoffen.sm-queue
launchctl bootout gui/$uid/$OLD
launchctl print gui/$uid/$OLD 2>&1 | head -1             # must say: Could not find service
mv ~/Library/LaunchAgents/$OLD.plist ~/Library/LaunchAgents/$OLD.plist.off   # or the next login loads it again
# the checked-in plist is a template: /Users/USER stands for the real home
sed "s#/Users/USER#$HOME#g" ~/Code/steuermaschine-stable/current/src/plugins/heinzel/launchd/$NEW.plist > ~/Library/LaunchAgents/$NEW.plist
! grep -q /Users/USER ~/Library/LaunchAgents/$NEW.plist
plutil -lint ~/Library/LaunchAgents/$NEW.plist
launchctl bootstrap gui/$uid ~/Library/LaunchAgents/$NEW.plist
launchctl print gui/$uid/$NEW | grep -E 'state =|pid ='  # state = running, a pid
```

Then set `queue_service_label = "<NEW>"` in the `[heinzel]` table of the user config and in
`~/.config/steuermaschine/migrate-values.toml` (so the next promote carries it), and check:
`sm self config | grep queue_service_label` names the new label.

## 3. Back

The reverse of step 2: boot out `$NEW`, remove its plist, rename `$OLD.plist.off` back, bootstrap `$OLD`, and set
`queue_service_label` back to `$OLD` in both files.

## 4. After every promote

The loop ends by itself when the stable pointer names another release; launchd (KeepAlive) starts it on the new one.
Check `launchctl print gui/$uid/$NEW | grep 'pid ='` shows a new pid within a minute. A promote that changes
`native/queue.py` or `queue_lib/` voids the queue's verdicts: a loop on such a release exits 70 every 30 seconds and
merges nothing, so check `sm self reviews` before promoting.
