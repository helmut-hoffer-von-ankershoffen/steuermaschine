"""Shipping one head: push check, PR route, guard-then-queue."""

from ..queue_lib import edge
from . import moved_map

edge.MAPS.setdefault("ship_lib", moved_map)  # a moved tool that loads another moved tool finds the ones of this directory too
