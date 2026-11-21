"""`sm release status` after the tag: the tail stages with their ages, not `PR #None` and `ok ? fail ?`."""

import calendar
import json
import time

from steuermaschine_heinzel.native.release_read_lib import status

NOW = calendar.timegm(time.strptime("2026-11-21T12:00:00Z", "%Y-%m-%dT%H:%M:%SZ"))
RECEIPT = {"version": "0.9.53", "pypi": "2026-11-21T11:30:00Z", "pypi_run_id": 11, "compose": "2026-11-21T09:50:00Z",
           "compose_run_id": 22, "acceptance": "2026-11-21T11:55:00Z", "acceptance_requested": True}


def test_tail_stages_list_every_stage_with_state_and_age():
    lines = status.tail_stages(RECEIPT, NOW)
    assert [ln.split()[0] for ln in lines] == ["publish", "PyPI", "compose", "acceptance", "marker", "Mini", "iOS"]
    assert "run 11, 30m ago" in lines[1] and "run 22, 2h10m ago" in lines[2]
    assert "running, 5m ago" in lines[3] and "not started" in lines[0] and "not started" in lines[6]


def test_finished_acceptance_reads_accepted():
    lines = status.tail_stages(RECEIPT | {"finished": True, "accepted": True, "acceptance_run": 33}, NOW)
    assert "ACCEPTED" in lines[3] and "NOT" not in lines[3]


def test_render_after_the_tag_has_no_pr_none_and_no_question_marks(tmp_path):
    (tmp_path / "v0.9.53.json").write_text(json.dumps(RECEIPT))
    assert status.read_tail("0.9.54", str(tmp_path)) is None
    tail = status.read_tail("0.9.53", str(tmp_path))
    summary = {"active": True, "version": "0.9.53", "pr": None, "phase": "tail", "age_min": 90, "frozen": False, "next": "wait",
               "tail": status.tail_stages(tail, NOW)}
    text = status.render(summary)
    assert "PR #None" not in text and "?" not in text and "tail stages:" in text and "PyPI" in text
