"""The native commands of the plugin, one module per area (docs/INTEGRATION.md, section 1).

To add an area: import its module here and append it to MODULES. Nothing else belongs in this file.
"""

from typing import Any

from steuermaschine.native import collect

from . import (
    affected,
    agents,
    alarm,
    alarm_asks,
    asks,
    branches,
    briefs,
    check,
    ci_classify,
    ci_evidence,
    ci_ops,
    ci_read,
    ci_triage,
    combine,
    coord,
    dispatch,
    docs,
    flakes,
    go,
    guards,
    handoff,
    junit,
    leakwatch,
    loadguard,
    lock,
    loops,
    merge_ledger,
    merge_ready,
    merges,
    mini,
    plugin_gallery,
    preflight,
    queue,
    queue_hold,
    queue_main,
    refill,
    rejudge,
    release,
    release_read,
    ship,
    ship_driver,
    skills,
    slots,
    status,
    sweep,
    tasks,
    tick,
    train,
    velocity,
    why,
    work,
)

MODULES: list[Any] = [
    affected, agents, alarm, alarm_asks, asks, branches, briefs, check, ci_classify, ci_evidence, ci_ops, ci_read, ci_triage, combine,
    coord, dispatch, docs, flakes, go, guards, handoff, junit, leakwatch, loadguard, loops, lock, merge_ledger, merge_ready, merges, mini,
    plugin_gallery, preflight, queue, queue_hold, queue_main, refill, rejudge, release, release_read, ship, ship_driver, skills, slots, status, sweep,
    tasks, tick, train, velocity, why, work,
]


def commands() -> list[Any]:
    return collect(*MODULES)
