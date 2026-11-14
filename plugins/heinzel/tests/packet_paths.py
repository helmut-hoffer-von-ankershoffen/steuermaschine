"""Where the moved packet tests find the package directory and its modules (they used to load tool scripts by path)."""

import importlib
from pathlib import Path

import pytest

import steuermaschine_heinzel.native.packet_lib as _lib

TOOLS = Path(_lib.__file__).resolve().parent


@pytest.fixture
def tool():
    return lambda name: importlib.import_module(f"{_lib.__name__}.{name}")
