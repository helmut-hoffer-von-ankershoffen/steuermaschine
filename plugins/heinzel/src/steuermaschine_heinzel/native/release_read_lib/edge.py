"""The edges of the moved read-only release code: the same as the release code's (checkouts, the process runner)."""

from __future__ import annotations

from ..queue_lib import edge as _base
from ..release_lib.edge import PACKAGE, configure, configure_release, core_checkout, fresh, records_dir, run  # noqa: F401 - the moved code's edge
from . import moved_map

_base.MAPS.setdefault("release_read_lib", moved_map)  # a test asks for a moved tool by its old file name
