"""Pure helpers of the refill: free builder slots, dead owners, suggested model per ready bead. Moved from the former
refill helpers unchanged (check: plugins/heinzel/tests/coord_verbatim.py); `BEAD` and `bead_of` come from the former
digest module."""
# mypy: disable-error-code="no-untyped-def,no-untyped-call"
import re
from datetime import datetime, timezone

BEAD = re.compile(r"heinzel-management-([a-z0-9]{3})|\b(?:bead|bd)[ -]([a-z0-9]{3})\b|/([a-z0-9]{3})-", re.I)


def bead_of(pr):
    m = BEAD.search(pr.get("headRefName", "") or "") or BEAD.search(pr.get("title", "") or "")
    return next((g for g in m.groups() if g), None) if m else None


TARGET = 12
LOAD_MAX = 25


def _age_min(ts, now):
    return int((now - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() // 60)


def suggest_model(bead):
    """Haiku chores/docs, Sonnet small UI, Opus install/update/auth/debug, Fable design."""
    text = (bead.get("title", "") + " " + " ".join(bead.get("labels") or [])).lower()
    has = lambda *w: any(re.search(r"\b" + x, text) for x in w)
    if has("design", "mockup", "ux spec"):
        return "Fable"
    if has("install", "update", "updater", "auth", "sign-in", "login", "flak", "debug", "recover", "repair", "daemon", "hang"):
        return "Opus"
    if has("chore", "doc", "docs", "meta", "close", "note", "typo", "readme"):
        return "Haiku"
    return "Sonnet"


def short(bead_id):
    return bead_id.split("-")[-1]


def agent_ids(text):
    """bead short id -> agent id, from research/agent-ids.md rows (last row wins)."""
    out = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and re.fullmatch(r"a[0-9a-f]{16}", cells[2]):
            for b in re.split(r"[,\s]+", cells[0]):
                if b:
                    out[b] = cells[2]
    return out


def dead_owners(in_progress, pr_updates, ids, now=None, stale_min=90):
    """In-progress beads with no bead update and no PR push for stale_min minutes.
    pr_updates: bead short id -> latest PR updatedAt (ISO)."""
    now = now or datetime.now(timezone.utc)
    out = []
    for b in in_progress:
        s = short(b["id"])
        ages = [_age_min(b["updated_at"], now)]
        if s in pr_updates:
            ages.append(_age_min(pr_updates[s], now))
        if min(ages) > stale_min:
            out.append(f"DEAD {s} idle {min(ages)} min -> resume {ids.get(s, 'no agent id')} or restart: {b['title'][:50]}")
    return out


def free_slots(in_progress, load, target=TARGET, load_max=LOAD_MAX):
    return 0 if load >= load_max else max(0, target - len(in_progress))


def start_lines(ready, in_progress, slots):
    busy = {b["id"] for b in in_progress}
    pick = [b for b in sorted(ready, key=lambda b: b.get("priority", 9)) if b["id"] not in busy and not (b.get("metadata") or {}).get("merged_in")]
    return [f"START {short(b['id'])} P{b.get('priority', '?')} {suggest_model(b)}: {b['title'][:80]}" for b in pick[:slots]]


def refill_line(slots, ready_n):
    return f"REFILL: {slots} free slots, {ready_n} ready" if slots > 0 else ""
