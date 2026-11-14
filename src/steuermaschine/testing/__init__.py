"""Fakes and the plugin conformance kit. Plugins use these in their own tests: `from steuermaschine.testing import ...`."""

from .conformance import assert_conformant, check_plugin
from .fakes import (
                    FakeAdmission,
                    FakeCI,
                    FakeClock,
                    FakeCommands,
                    FakeForge,
                    FakeQueue,
                    FakeRecipe,
                    FakeTracker,
                    FakeVcs,
                    FakeVerifier,
                    MemoryBriefs,
                    MemoryStore,
                    RecordingNotifier,
                    fake_providers,
                    invoke,
)

__all__ = [
                    "FakeAdmission",
                    "FakeCI",
                    "FakeClock",
                    "FakeCommands",
                    "FakeForge",
                    "FakeQueue",
                    "FakeRecipe",
                    "FakeTracker",
                    "FakeVcs",
                    "FakeVerifier",
                    "MemoryBriefs",
                    "MemoryStore",
                    "RecordingNotifier",
                    "assert_conformant",
                    "check_plugin",
                    "fake_providers",
                    "invoke",
]
