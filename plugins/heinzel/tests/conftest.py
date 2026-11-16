"""Plugin-wide test defaults.

The main plan check (`queue_lib/main_plan.gap`, bead q9kh) needs a Core checkout and GitHub; every queue test that is
not about it sees a main whose resulting tree is planned. Tests of the check itself request `real_main_plan`.
"""

from pathlib import Path

import pytest

from steuermaschine import config
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native.queue_lib import main_plan


def _records_for_tests():
    """The setting queue_records_dir has no default (a machine's checkout; production refuses, naming the key). The
    moved tests compute their paths when a module loads, as before: the configured checkout, else ~/Code/heinzel-management."""
    from steuermaschine_heinzel.native.queue_lib import edge

    return edge._records[0] if edge._records else Path("~/Code/heinzel-management").expanduser()


def _install_records_default():
    import importlib

    from steuermaschine_heinzel.native.queue_lib import edge

    edge.REFUSING_RECORDS_DIR = edge.records_dir  # the production function, for the test of its refusal
    for name in ("queue_lib", "ship_lib", "train_lib", "release_lib", "release_read_lib"):
        importlib.import_module(f"steuermaschine_heinzel.native.{name}.edge").records_dir = _records_for_tests


_install_records_default()


@pytest.fixture
def real_main_plan():
    """Opt out of the planned-main default: the test drives main_plan itself."""
    return main_plan


@pytest.fixture(autouse=True)
def _planned_main(request, monkeypatch):
    if "real_main_plan" not in request.fixturenames:
        monkeypatch.setattr(main_plan, "gap", lambda pr, head="": "")
    yield


@pytest.fixture
def real_state_root():
    """Opt out of the state directory under HOME: the test drives statedir.root itself."""
    return statedir.root


@pytest.fixture(autouse=True)
def _state_under_home(request, monkeypatch):
    """The plugin's state directory under the test's HOME, resolved at each call (a test that moves HOME moves it), so
    a test lays out state files with `statedir.root() / NAME` exactly where the code reads them."""
    if "real_state_root" not in request.fixturenames:
        def root():
            base = config.state_home() / "heinzel"
            base.mkdir(parents=True, exist_ok=True)
            return base

        monkeypatch.setattr(statedir, "root", root)
    yield


@pytest.fixture(autouse=True)
def core_policy_at_main(monkeypatch):
    """Core's release policy as the readers see it on GitHub: the kit's (old CI shape) at any commit, never a network
    read. A test of the readers themselves calls `core_facts.real` or patches these again."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import policy_kit as kit

    from steuermaschine_heinzel.native.queue_lib import core_facts

    core_facts.forget()
    monkeypatch.setattr(core_facts, "main_sha", lambda: "c" * 40)
    monkeypatch.setattr(core_facts, "at", lambda commit: kit.policy(commit=commit))
