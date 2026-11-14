"""What the moved tests import by name: the loader of a moved tool and the tools directory the old tests knew."""

from pathlib import Path

import pytest
from steuermaschine_heinzel.native.queue_lib import edge

TOOLS = Path(edge.__file__).resolve().parent  # what the old tests called the tools directory: where the moved modules are


def load(name: str):
    dotted = edge.module_of(name)
    if dotted is None or not edge.whole(name):
        pytest.skip(f"{name} as a whole has not moved into the plugin (yet, or it belongs to another slice)")
    module = edge.fresh(dotted)
    if hasattr(module, "publish_acceptance"):  # no test writes the public acceptance marker; its own tests use real_
        module.real_publish_acceptance = module.publish_acceptance
        module.publish_acceptance = lambda *_: None
    if hasattr(module, "promote_release"):  # no test edits a public GitHub release; its own tests use real_
        module.real_promote_release = module.promote_release
        module.promote_release = lambda *_: None
    return module


def unmoved(module) -> bytes:
    """The source of a moved module as it was before the move: without the edge import and with the plain process
    call. Used to show that the reader this package runs is, edge lines aside, the reviewed recorder."""
    lines = [line for line in Path(module.__file__).read_text().splitlines(keepends=True) if not line.startswith("from . import edge")]
    return "".join(lines).replace("edge.run(", "subprocess.run(").encode()
