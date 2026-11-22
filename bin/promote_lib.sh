# promote_lib.sh: the parts of bin/promote that decide when to switch, sourced by it (and by its tests).
# Functions only; nothing runs when this file is sourced.

# The admission and release steps that must not see the live `sm` change under them, as `ps` lines (at most 5).
# The same steps as WRITE_STEPS of the plugin's guard_floor.py; tests/test_promote_script.py holds the two equal.
busy_steps() {
  ps -axo pid=,command= | grep -E '(steuermaschine|/sm|sm-dev)( --format[ =][a-z]+)? (head (prepare|apply|admit|validate)|release (admit|resume|cut)|ship( |$))|hz-focused-admit (prepare|apply)|hz-release( |$)|hz_focused_admit' | grep -v grep | head -5
}

# wait_idle MAX_SECONDS POLL_SECONDS: 0 once no step runs; 1 when one still runs after MAX_SECONDS (its lines on stdout).
# A running step is waited for, not refused: the gate, build and smoke already passed and stay valid for this commit.
wait_idle() {
  local max="$1" poll="$2" waited=0 told=0 busy
  while busy="$(busy_steps)"; [ -n "$busy" ]; do
    if [ "$waited" -ge "$max" ]; then echo "$busy"; return 1; fi
    if [ "$told" = 0 ]; then told=1; echo "promote: a write step is running; waiting up to ${max}s for it to end, then switching:" >&2; echo "$busy" | sed 's/^/    /' >&2; fi
    sleep "$poll"; waited=$((waited + poll))
  done
  [ "$told" = 0 ] || echo "promote: the write step ended after ${waited}s" >&2
  return 0
}

# The switch-pending file: while it names a live promote, the Bash hook (guard_floor.switch_refusal) refuses new
# admission and release steps, so the running ones can end and none starts under the switch.
pending_file() { echo "${HOME}/.cache/sm-promote-switch-pending"; }
mark_switch_pending() { mkdir -p "$(dirname "$(pending_file)")" && echo "$$ $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$(pending_file)"; }
clear_switch_pending() {  # only our own file: another promote's is not ours to remove
  local f; f="$(pending_file)"
  [ -f "$f" ] && [ "$(cut -d' ' -f1 "$f")" = "$$" ] && rm -f "$f"
  return 0
}

# The quality gate's receipt for one commit: written only when bin/check was green, so a retry of the same candidate
# (after a refused switch) does not repeat the 7-minute gate. Keyed by the full commit SHA of a clean checkout.
gate_receipt() { echo "$1/gate-passed/$2"; }           # gate_receipt STABLE SHA
gate_passed() { [ -f "$(gate_receipt "$1" "$2")" ]; }  # gate_passed STABLE SHA
record_gate() {                                         # record_gate STABLE SHA
  mkdir -p "$1/gate-passed" && date -u +%Y-%m-%dT%H:%M:%SZ > "$(gate_receipt "$1" "$2")"
}

# A red gate let through with --red-gate-ok: its reason, kept next to the receipts (one line per acceptance: when, the
# commit, the reason, the red gates). It is evidence, not a receipt: gate_passed never reads it, so a retry runs the gate.
red_gate_log() { echo "$1/gate-red-accepted"; }        # red_gate_log STABLE
record_red_gate() {                                     # record_red_gate STABLE SHA REASON RED_GATES
  mkdir -p "$1" && printf '%s %s reason=%s red=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" "$3" "$4" >> "$(red_gate_log "$1")"
}

# verdict_holds PYTHON STABLE REPO SHA CANDIDATE_CHECKER: 0 when the promote verdict on SHA holds. The candidate's own
# checker decides, and so does the live release's checker (STABLE/current/src/bin/promote_verdict.py) when it has one:
# a candidate that weakens or breaks its own checker is still judged by the reviewed one. Only before the first release
# with a checker does the candidate's copy decide alone (bootstrap; it says so). Messages go to stderr.
verdict_holds() {
  local python="$1" stable="$2" repo="$3" sha="$4" cand="$5" live="$2/current/src/bin/promote_verdict.py" rc=0
  "$python" -I "$cand" check "$stable" "$repo" "$sha" || rc=1
  if [ -f "$live" ]; then
    "$python" -I "$live" check "$stable" "$repo" "$sha" || { echo "verdict: the live release's checker refuses it" >&2; rc=1; }
  else
    echo "verdict: the live release has no promote_verdict.py: the candidate's checker alone decides (bootstrap)" >&2
  fi
  return "$rc"
}

# The failing test ids of a bin/check output (pytest's short summary), so a refused promote names them (bead t84w:
# a refusal that said only "check: FAIL tests" left the flaky test unknown). At most 20.
failed_tests() { grep -oE '(FAILED|ERROR) [^ ]+::[^ ]+' "$1" | head -20; }

# migrate_state_to TARGET TARGET_CONFIG [LIVE LIVE_CONFIG]: move the state files to the names the TARGET release uses,
# before the switch to it. TARGET and LIVE are the `steuermaschine` programs of two releases, each run with its own
# user config. LIVE (the release that runs now) first moves back what only it knows (`--back --keep` the target's
# list), then TARGET moves forward what it knows. A release without `self migrate-state` knows no moves. Each step
# logs every rename (also to state-migrations.log in the project's state directory); any refusal returns 1.
# Run it only while the switch-pending file is written and no write step runs (wait_idle).
knows_moves() { [ -x "$1" ] && STEUERMASCHINE_USER_CONFIG="$2" "$1" self migrate-state --help >/dev/null 2>&1; }
migrate_state_to() {
  local target="$1" target_config="$2" live="${3:-}" live_config="${4:-}" keep
  keep="$(mktemp)" || return 1
  if knows_moves "$target" "$target_config"; then
    STEUERMASCHINE_USER_CONFIG="$target_config" "$target" --format json self migrate-state --list > "$keep" || { rm -f "$keep"; return 1; }
  else echo '{"moves": []}' > "$keep"; fi
  if [ -n "$live" ] && [ "$live" != "$target" ] && knows_moves "$live" "$live_config"; then
    STEUERMASCHINE_USER_CONFIG="$live_config" "$live" self migrate-state --back --keep "$keep" || { rm -f "$keep"; return 1; }
  fi
  rm -f "$keep"
  if knows_moves "$target" "$target_config"; then STEUERMASCHINE_USER_CONFIG="$target_config" "$target" self migrate-state || return 1; fi
  return 0
}

# install_launcher STABLE: write STABLE/bin/sm, the launcher every `sm` call goes through (atomic rename). It resolves
# `current` once per process tree and carries the release and its user config to every sm that sm starts.
install_launcher() {
  mkdir -p "$1/bin" || return 1
  cat > "$1/bin/.sm.$$" <<'SH'
#!/bin/sh
# sm: the STABLE steuermaschine. It changes through release_ci.py follow (canary, swap, rollback), bin/promote and bin/rollback.
# `current` is resolved ONCE per process tree: STEUERMASCHINE_PINNED carries the release to every sm a running sm starts,
# so a switch never mixes two releases (or a new config with old code) in one tree.
root="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$0")")/.." && pwd)"
# A pin counts only when it is one release of THIS install (a direct child of releases/): anything else is ignored.
rel=""; releases="$(cd "$root/releases" 2>/dev/null && pwd -P)"
if [ -n "${STEUERMASCHINE_PINNED:-}" ] && [ -n "$releases" ]; then
  pin="$(cd "$STEUERMASCHINE_PINNED" 2>/dev/null && pwd -P)"
  case "$pin" in "$releases"/*/*|"") ;; "$releases"/*) [ -x "$pin/venv/bin/steuermaschine" ] && rel="$pin" ;; esac
fi
# The canary (release_ci.py follow): only an sm started with STEUERMASCHINE_CANARY=1 runs it, and only a release of THIS install.
if [ -z "$rel" ] && [ "${STEUERMASCHINE_CANARY:-}" = 1 ] && [ -L "$root/canary" ] && [ -n "$releases" ]; then
  can="$(cd "$root/canary" 2>/dev/null && pwd -P)"
  case "$can" in "$releases"/*/*|"") ;; "$releases"/*) [ -x "$can/venv/bin/steuermaschine" ] && rel="$can" ;; esac
fi
[ -n "$rel" ] || rel="$(cd "$root/current" && pwd -P)" || exit 70
export STEUERMASCHINE_PINNED="$rel"
if [ -f "$rel/user-config.toml" ]; then export STEUERMASCHINE_USER_CONFIG="$rel/user-config.toml"; fi   # code and config switch together
export STEUERMASCHINE_RELEASE="$root/current"   # a running loop ends when this pointer names another release
"$rel/venv/bin/steuermaschine" "$@"
rc=$?
# A tool error is evidence for the canary verdict and the tool-stall alarm (release_ci.py follow): `EPOCH RELEASE 70`.
if [ "$rc" = 70 ]; then printf '%s %s %s\n' "$(date +%s)" "${rel##*/}" "$rc" >> "$root/exits.log" 2>/dev/null; fi
exit "$rc"
SH
  chmod +x "$1/bin/.sm.$$" && mv -f "$1/bin/.sm.$$" "$1/bin/sm"
}

# point_current STABLE ID: current -> releases/ID; previous -> what current named before (unless it is the same).
# Each pointer is a new symlink renamed over the old one: a reader sees the old or the new release, never none.
point_current() {
  python3 - "$1" "$2" <<'PY'
import os, sys
stable, new = sys.argv[1], sys.argv[2]
cur = os.path.join(stable, "current")
old = os.readlink(cur) if os.path.islink(cur) else None
def point(name, target):  # atomic: a new symlink renamed over the old one
    tmp = os.path.join(stable, f".{name}.{os.getpid()}")
    os.symlink(target, tmp)
    os.replace(tmp, os.path.join(stable, name))
if old and old != f"releases/{new}":
    point("previous", old)
point("current", f"releases/{new}")
PY
}
