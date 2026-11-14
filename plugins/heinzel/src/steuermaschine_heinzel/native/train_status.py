"""`sm release train status`: what the moved train's status prints, as a typed result. Read-only: this module is the
review-free unit of that command, and it calls only the train's reading functions."""

from __future__ import annotations

import json
from typing import Any

from .train_lib.names import translated


def status(module: Any) -> Any:
    from .train import TrainRow, TrainStatus

    world = module.Real()
    text = module.status(world)
    head, _, tail = text.partition("\n{")
    lines = translated([line for line in head.splitlines() if line], module.STATE_DIR)
    state = json.loads("{" + tail) if tail else json.loads(text) if text.lstrip().startswith("{") else {}
    rows = [TrainRow(version=str(t["version"]), phase=str(t.get("phase") or ""), running=module.lock_busy(world, f"v{t['version']}.lock"),
                     attempts=int(t.get("attempts", 0) or 0), reason=str(t.get("reason") or "")) for t in module.active_trains()]
    candidate = next((line.split()[0] for line in lines if " candidate: " in line), "")
    return TrainStatus(trains=rows, tails=[line for line in lines if " tail: " in line], candidate=candidate, state=state,
                       lines=[*lines, *translated(json.dumps(state, indent=1, sort_keys=True).splitlines(), module.STATE_DIR)])
