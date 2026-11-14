"""Fast stuck-queued receipt guard: list rows at every guard, one parallel full proof before the one write.

Change C of research/fast-fix-recut-2026-11-13.md. The zombie pull_request runs that GitHub can neither cancel nor
delete never change: `updated_at == created_at == run_started_at`, attempt 1, no conclusion, no jobs. A run that gets
jobs is touched (`updated_at` moves when GitHub creates them). So every guard except the one directly before the slot
write accepts a stuck-queued receipt from the complete active inventory's own list row, with no per-run call and no
cancel POST. The guard directly before the write runs the full per-run proof of `focused_slots` (run GET, zero jobs,
the exact 409 when probing), on bounded threads.

Weakens, stated plainly: at the other guards "no jobs" is inferred from the untouched timestamps instead of read from
the jobs endpoint. That GitHub touches `updated_at` when it creates jobs is observed, not documented. The pre-write
guard does not rely on it. A stuck-queued receipt whose run is no longer in the active inventory refuses.
"""
from __future__ import annotations

import calendar
import time
from concurrent.futures import ThreadPoolExecutor

WORKERS = 8


def row_failure(rt, row, pin, registries, *, paths, min_age, stamp):
    """None when this list row is still an untouched harmless zombie, else the refusal reason."""
    if any(row.get(k) != v for k, v in pin.items()) or row.get('id') is None:
        return 'identity changed'
    if row.get('status') != 'queued' or row.get('conclusion') is not None:
        return 'run left queued'
    if not (isinstance(row.get('repository'), dict) and row['repository'].get('full_name') == rt.repo):
        return 'repository changed'
    if row.get('event') != 'pull_request' or row.get('path') not in paths:
        return 'outside the pull_request allowlist'
    started = row.get('run_started_at')
    if not (isinstance(started, str) and stamp.fullmatch(started)) or \
            rt.now() - calendar.timegm(time.strptime(started, '%Y-%m-%dT%H:%M:%SZ')) < min_age:
        return 'younger than 30 minutes'
    # Never touched since creation: no job was ever created, no attempt was re-queued.
    if not (row.get('updated_at') == row.get('created_at') == started):
        return 'run was touched (updated_at, created_at and run_started_at differ)'
    if any(pin['head_sha'] in text for text in registries):
        return 'head has a focused plan'
    return None


def vanished(active, exceptions, kind):
    """Stuck-queued receipt runs absent from the active inventory, sorted (the list no longer shows them queued)."""
    return sorted(run_id for run_id, proof in exceptions.items() if proof.get('kind') == kind and run_id not in active)


def parallel(fn, items):
    """fn over items on at most WORKERS threads; results in item order. The first refusal in item order is raised
    after every submitted item has finished, so no thread outlives the guard."""
    items = list(items)
    if len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        futures = [pool.submit(fn, item) for item in items]
        return [future.result() for future in futures]


def pre_write(written):
    """Callable for an apply guard: True only for the guard directly before the one slot write.

    `apply_slots` calls its guard before the registry read, again directly before the write, and after the readback.
    The second call while nothing is written is the pre-write guard (when the transaction changes nothing, the second
    call is the read-only guard after the readback; a full proof there costs time, not safety).
    """
    calls = 0

    def full():
        nonlocal calls
        calls += 1
        return calls == 2 and not written()
    return full
