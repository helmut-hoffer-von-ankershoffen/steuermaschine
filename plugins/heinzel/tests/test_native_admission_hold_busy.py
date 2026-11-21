"""The admission hold's refusal when the window is locked, and the apply tracker's pass-through of a written-without-receipt refusal."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from steuermaschine.engine import Refuse
from steuermaschine.protocols import Busy
from steuermaschine_heinzel.native import window
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import hold, wait


def test_a_locked_window_refuses_the_hold_and_a_failing_unhold_never_masks_the_result(monkeypatch):
    class Line:
        def __init__(self, busy_on):
            self.busy_on = busy_on

        def hold(self, *a, **kw):
            if self.busy_on == "hold":
                raise Busy("locked")

        def unhold(self, owner):
            if self.busy_on == "unhold":
                raise Busy("locked")

    monkeypatch.setattr(window, "the_window", lambda rt: Line("hold"))
    with pytest.raises(Refuse, match="cannot register the admission hold"), hold.admission_hold(SimpleNamespace(), "release admit prepare p"):
        pytest.fail("the block must not run")
    monkeypatch.setattr(window, "the_window", lambda rt: Line("unhold"))
    with hold.admission_hold(SimpleNamespace(), "release admit apply p"):
        pass  # the lapsed hold is not an error


def test_a_written_without_receipt_refusal_passes_through_the_tracker_unchanged():
    rt = SimpleNamespace(set_variable=lambda name, value: None)

    def run():
        raise wait.WrittenWithoutReceipt("slot written, guard refused")

    with pytest.raises(wait.WrittenWithoutReceipt, match="slot written"):
        wait.tracked(rt, run)
    with pytest.raises(a.AdmissionError, match="plain refusal"):
        wait.tracked(rt, lambda: (_ for _ in ()).throw(a.AdmissionError("plain refusal")))


def test_a_run_of_another_repository_is_not_a_harmless_zombie():
    from steuermaschine_heinzel.native.admission_lib import receipt_guard

    rt = SimpleNamespace(repo="o/core", now=lambda: 0)
    row = {"id": 1, "status": "queued", "conclusion": None, "repository": {"full_name": "other/x"}}
    assert receipt_guard.row_failure(rt, row, {}, [], paths=set(), min_age=0, stamp=None) == "repository changed"
