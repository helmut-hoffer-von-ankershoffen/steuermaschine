"""The stage table of `sm why VERSION`: pure rows from facts, the recorded rows that outlive expired runs, the LIVE-OK mark."""

from steuermaschine_heinzel.native import release_stages as st


def run(a, b, conclusion="success", jobs=None):
    return {"createdAt": f"2026-11-22T{a}Z", "updatedAt": f"2026-11-22T{b}Z", "conclusion": conclusion, **({"jobs": jobs} if jobs else {})}


FACTS = st.Facts(
    ci=[run("03:00:00", "03:20:00"), run("03:01:00", "03:30:00")], merged_at="2026-11-22T03:35:00Z", released_at="2026-11-22T04:13:00Z",
    pypi=run("04:15:00", "04:16:00"), compose=run("04:15:30", "04:20:00"),
    acceptance=run("04:20:00", "04:46:00", jobs=[{"name": "web", "startedAt": "2026-11-22T04:21:00Z", "completedAt": "2026-11-22T04:40:00Z", "conclusion": "success"},
                                                {"name": "core", "startedAt": "2026-11-22T04:21:00Z", "completedAt": "2026-11-22T04:46:00Z", "conclusion": "success"}]),
    live_at="2026-11-22T04:50:00Z", ios={"started": 1795322400.0, "at": "2026-11-22T05:10:00+00:00", "clean": True})


def test_every_stage_has_its_row_with_durations_and_one_row_per_shard():
    rows = {r.name: r for r in st.stage_rows(FACTS)}
    assert rows["candidate CI"].seconds == 30 * 60 and rows["merge"].seconds == 5 * 60 and rows["tag and release"].seconds == 38 * 60
    assert rows["PyPI"].seconds == 60 and rows["compose"].seconds == 270 and rows["acceptance"].seconds == 26 * 60
    assert rows["  shard web"].seconds == 19 * 60 and rows["  shard core"].seconds == 25 * 60
    assert rows["Mini follow"].seconds == 4 * 60 and rows["Mini follow"].result == "LIVE-OK" and rows["iOS (TestFlight)"].result == "clean"


def test_the_total_runs_from_the_first_start_to_live_ok_and_renders_in_the_table():
    rows = st.stage_rows(FACTS)
    assert st.total_seconds(rows) == 110 * 60  # 03:00 to 04:50
    assert st.render(rows)[-1].startswith("TOTAL cut to LIVE-OK") and "110m00s" in st.render(rows)[-1]
    assert any("shard core" in line and "25m00s" in line for line in st.render(rows))


def test_a_missing_fact_is_a_dash_not_a_failure():
    rows = st.stage_rows(st.Facts(ci=[], merged_at="", released_at="", pypi=None, compose=None, acceptance=None, live_at="", ios=None))
    assert all(r.seconds is None for r in rows) and st.total_seconds(rows) is None
    assert all("-" in line for line in st.render(rows)[:-1])


def test_the_record_keeps_rows_and_the_live_mark(tmp_path):
    rows = st.stage_rows(FACTS)
    st.mark_live(tmp_path, "0.9.54", 1795320000.0)
    saved = st.read_record(tmp_path, "0.9.54")
    assert saved["live_at"].endswith("Z")
    st.record(tmp_path, "0.9.54", rows, saved["live_at"])
    again = st.read_record(tmp_path, "0.9.54")
    assert [r["name"] for r in again["stages"]] == [r.name for r in rows] and again["live_at"] == saved["live_at"]


def test_why_version_prints_the_stage_table(monkeypatch):
    from steuermaschine_heinzel.native import why_subject
    from steuermaschine_heinzel.native.release_read import ReleaseStatus

    out = why_subject.WhyRelease(version="0.9.54", status=ReleaseStatus(active=False), stages=st.stage_rows(FACTS)).render()
    assert "STAGES" in out and any("candidate CI" in line for line in out) and out[-1].lstrip().startswith("TOTAL")
    monkeypatch.setattr(why_subject, "world", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    assert why_subject.stage_table("0.9.54") == []


class FakeWorld:
    """gh_json answers by the subcommand; a value that is an exception is raised."""

    repo = "owner/core"

    def __init__(self, state_dir, answers):
        self.state_dir, self.answers = state_dir, answers

    def gh_json(self, argv, timeout=0):
        answer = self.answers.get(argv[0], None)
        if isinstance(answer, Exception):
            raise answer
        return answer(argv) if callable(answer) else answer


def test_a_bad_timestamp_is_unknown_not_a_crash():
    assert st.parse("not a time") is None and st.parse("") is None
    assert st.Stage(name="x", start="bad", end="2026-11-22T03:00:00Z").seconds is None


def test_run_view_reads_only_integer_run_ids_and_survives_a_failed_call(tmp_path):
    good = FakeWorld(tmp_path, {"run": {"conclusion": "success"}})
    assert st.run_view(good, "o/r", None) is None and st.run_view(good, "o/r", "7") is None
    assert st.run_view(good, "o/r", 7) == {"conclusion": "success"}
    assert st.run_view(FakeWorld(tmp_path, {"run": RuntimeError("down")}), "o/r", 7) is None
    assert st.run_view(FakeWorld(tmp_path, {"run": ["not", "a", "dict"]}), "o/r", 7) is None


def test_pull_request_facts_pick_the_ci_runs_of_the_release_pr_head(tmp_path):
    def answer(argv):
        if argv[0] == "pr":
            return [{"title": "chore: v0.9.54", "headRefOid": "abc", "mergedAt": "2026-11-22T03:35:00Z"}]
        return [{"workflowName": "CI", "createdAt": "x"}, {"workflowName": "CodeQL"}]

    w = FakeWorld(tmp_path, {"pr": answer, "run": answer})
    runs, merged = st.pull_request_facts(w, "0.9.54")
    assert runs == [{"workflowName": "CI", "createdAt": "x"}] and merged == "2026-11-22T03:35:00Z"
    assert st.pull_request_facts(FakeWorld(tmp_path, {"pr": []}), "0.9.54") == ([], "")
    assert st.pull_request_facts(FakeWorld(tmp_path, {"pr": RuntimeError("down")}), "0.9.54") == ([], "")


def test_released_at_reads_the_publish_time_or_nothing(tmp_path):
    assert st.released_at(FakeWorld(tmp_path, {"release": {"publishedAt": "2026-11-22T04:13:00Z"}}), "0.9.54") == "2026-11-22T04:13:00Z"
    assert st.released_at(FakeWorld(tmp_path, {"release": RuntimeError("none")}), "0.9.54") == ""


def test_gather_keeps_the_recorded_row_of_an_expired_run(tmp_path):
    recorded = [r for r in st.stage_rows(FACTS)]
    st.record(tmp_path, "0.9.54", recorded, "2026-11-22T04:50:00Z")
    rows = st.gather(FakeWorld(tmp_path, {}), "0.9.54", {"pypi_run_id": 5})
    by_name = {r.name: r for r in rows}
    assert by_name["PyPI"].start == "2026-11-22T04:15:00Z" and by_name["candidate CI"].end.endswith("03:30:00Z")
    assert st.read_record(tmp_path, "0.9.54")["live_at"] == "2026-11-22T04:50:00Z"
