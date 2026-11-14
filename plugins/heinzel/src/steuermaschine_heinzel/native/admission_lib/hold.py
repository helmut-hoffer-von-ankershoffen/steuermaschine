"""The admission hold: while an admission prepares or applies, the validation window starts nothing new.

`sm head prepare`, `sm head apply` and the two `sm release admit` commands (so also `sm head admit` and the release cut,
which run them) register a hold in the validation window (`steuermaschine.window.Window.hold`) for the time of the command.
Validations that already run finish; a waiting `sm head validate` waits. The hold ends with the command, also when it
fails; a hold whose command was killed is ignored once its pid is gone (or its lease ran out).
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any

from steuermaschine.engine import Refuse
from steuermaschine.protocols import Busy


@contextmanager
def admission_hold(rt: Any, label: str) -> Iterator[None]:
    """Hold the validation window quiet for the time of the block. Refuses when the hold cannot be registered: an
    admission that does not stop the line would starve behind it again."""
    from .. import window  # imported when used: the window pulls in the dispatch library

    line, owner = window.the_window(rt), f"admit-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        line.hold(owner, label, pid=os.getpid(), host=socket.gethostname())
    except Busy:
        raise Refuse("cannot register the admission hold: the validation window's state is locked by another command", "sm ci window status") from None
    try:
        yield
    finally:
        with suppress(Busy):  # the hold then lapses with this process (its pid is gone) or its lease; never mask the command's own result
            line.unhold(owner)
