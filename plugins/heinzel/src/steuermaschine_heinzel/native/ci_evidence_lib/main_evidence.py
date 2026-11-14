"""Moved from the former main-evidence tool: the note lines that say what makes main's CI evidence unknown, or what
is ignored and why. Verbatim, except that the queue module is a parameter (the tool loaded it as a global);
tests/views_verbatim.py prints every differing line."""


def explain(runs, queue):
    """Reason lines for what makes main_state unknown or ignorable; empty if nothing notable."""
    if not isinstance(runs, list) or not runs:
        return ["no runs listed (gh failed or empty)"]
    why = []
    for r in runs:
        if r.get("batch_unknown"):
            why.append(f"run {r.get('databaseId')}: batch_unknown (admission/delegation proof missing or malformed)")
        elif not queue.valid_main_run(r):
            why.append(f"run {r.get('databaseId')}: malformed row (sha/status/conclusion)")
    done = [r for r in runs if r.get("status") == "completed"]
    if not done:
        why.append("no completed run yet (only pending runs)")
    for r in done:
        if r.get("conclusion") == "cancelled":
            if any(x.get("conclusion") == "success" for x in done[: done.index(r)]):
                why.append(f"run {r['databaseId']}: cancelled, ignored (newer green run)")
            elif queue.superseded_by_pending(runs, r, done):
                why.append(f"run {r['databaseId']}: cancelled, ignored (newer run pending)")
            else:
                why.append(f"run {r['databaseId']}: cancelled with no newer green or pending run -> UNKNOWN")
    return why
