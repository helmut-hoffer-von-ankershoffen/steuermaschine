"""hz-train next [--start] (tools/hz_next.py): the N+1 batch view at the cut, with its per-version record."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steuermaschine_heinzel.native.train_lib import batch_next as n


def test_next_version():
    assert n.next_version("0.9.31") == "0.9.32"


def test_queued_prs_only_exact_queued_rows():
    text = "661          queued      \n12  needs work\n(5 hidden)\n13 queued x"
    assert n.queued_prs(text) == [661]


def test_pr_ready_states():
    ok = {"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"}], "mergeStateStatus": "CLEAN"}
    assert n.pr_ready(ok)[0] == "green-current"
    assert n.pr_ready(ok | {"mergeStateStatus": "BEHIND"})[0] == "behind"
    assert n.pr_ready({"statusCheckRollup": [{"status": "IN_PROGRESS"}]})[0] == "running"
    assert n.pr_ready({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE", "name": "x"}]})[0] == "red"
    assert n.pr_ready({})[0] == "running"


def test_bump_time_and_blockers_and_nudges():
    prs = [{"title": "chore(release): v0.9.31", "mergedAt": "2026-11-08T05:30:00Z"}, {"title": "fix: x", "mergedAt": "2026-11-08T06:00:00Z"}]
    assert n.bump_time(prs, "0.9.31").hour == 5 and n.bump_time(prs, "0.9.32") is None
    assert n.blockers("quiet\nMAIN RED after batch\nRELEASE v1 freeze off", ["f"]) == ["hold/freeze file: f", "MAIN RED after batch"]
    assert n.nudges("55 in progress\nDEAD a idle\nSTART b x") == ["DEAD a idle", "START b x"]


def fake_sh(calls):
    def sh(cmd, timeout=90):
        calls.append(cmd)
        if cmd.startswith("gh pr list"):
            return json.dumps([{"number": 1, "title": "chore(release): v0.9.31", "mergedAt": "2026-11-08T05:30:00Z"},
                               {"number": 2, "title": "fix: after", "mergedAt": "2026-11-08T06:30:00Z"},
                               {"number": 3, "title": "fix: before", "mergedAt": "2026-11-08T05:00:00Z"}])
        if cmd.startswith("sm queue list"):
            return "7 queued\n"
        if cmd.startswith("gh pr view"):
            return json.dumps({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS", "name": "a"}], "mergeStateStatus": "CLEAN"})
        if cmd.startswith("hz-announce-merge"):
            return "GO 8\n"
        return ""
    return sh


def test_view_is_read_only_and_start_records(tmp_path, capsys):
    calls = []
    trains = [{"version": "0.9.31", "phase": "release", "start": 1.0}]
    assert n.run(tmp_path, trains, sh=fake_sh(calls), now=1000.0) == 0
    out = capsys.readouterr().out
    assert "v0.9.32 batch is OPEN" in out and "#2" in out and "#3" not in out and "1 of 1 queued" in out
    assert not any(c.startswith(("sm queue add", "hz-announce-merge")) for c in calls) and not list(tmp_path.iterdir())
    assert n.run(tmp_path, trains, start=True, sh=fake_sh(calls), now=1000.0) == 0
    out = capsys.readouterr().out
    assert "next: v0.9.32 batch open, 1 merged, 1 queued" in out and "GO 8" not in out and not any(c.startswith("hz-announce-merge") for c in calls)
    assert "sm queue add 7" in calls
    rec = json.loads((tmp_path / "next-v0.9.32.json").read_text())
    assert rec["green"] == [7] and rec["cut"] == "0.9.31" and rec["started"] is True


def test_no_cut_train_does_nothing(tmp_path, capsys):
    assert n.run(tmp_path, [{"version": "0.9.30", "phase": "cleared"}], start=True, sh=fake_sh([])) == 0
    assert "no train has been cut" in capsys.readouterr().out and not list(tmp_path.iterdir())


def test_record_keeps_first_opened_at(tmp_path):
    n.record(tmp_path, "1.0.1", {"updated_at": "a"})
    n.record(tmp_path, "1.0.1", {"updated_at": "b"})
    assert json.loads(n.record_path(tmp_path, "1.0.1").read_text()) == {"opened_at": "a", "updated_at": "b"}
