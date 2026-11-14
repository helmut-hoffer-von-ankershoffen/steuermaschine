"""heinzel-management-ovvg: `sm release plugin` binds to the run of its own dispatch.

It once stored only a dispatch time and adopted the newest release run of ANY plugin (tailscale's success reported as
google's after google's own run failed), and it never dispatched again. Now: the dispatch records its run ID, a rerun
follows only that ID, success needs the run's RELEASE_TAG, and a failed run is dispatched again only with --redispatch.
"""

import base64
import json
import subprocess

import pytest

from steuermaschine_heinzel.native.queue_lib import edge, hzlib

SHA, MOVED = "a" * 40, "b" * 40
GOOGLE, TAILSCALE, AGAIN = 43486690355, 43487675727, 43490000001
WORKFLOW = ".github/workflows/release.yml"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeGh:
    """The GitHub replies the plugin release reads; every call is kept."""

    def __init__(self):
        self.calls, self.runs, self.tags, self.dispatch_ids = [], {}, {}, [GOOGLE]
        self.listed = [{"databaseId": TAILSCALE, "status": "completed", "conclusion": "success", "createdAt": "2099-01-01T00:00:00Z",
                        "headSha": SHA, "displayTitle": "Release one plugin", "attempt": 1, "event": "workflow_dispatch"}]

    def run(self, run_id, status="in_progress", conclusion=None, tag=None, head=SHA):
        self.runs[run_id] = {"id": run_id, "path": WORKFLOW, "event": "workflow_dispatch", "head_sha": head, "status": status,
                             "conclusion": conclusion, "created_at": "2026-11-13T18:51:44Z", "display_title": "Release one plugin", "run_attempt": 1}
        if tag:
            self.tags[run_id] = tag

    def __call__(self, *a):
        self.calls.append(a)
        toml = base64.b64encode(b'[project]\nname = "heinzel-google"\nversion = "0.9.4"\n').decode()
        if "contents/" in a[1]:
            return 0, toml
        if a[1].endswith("/commits/main"):
            return 0, SHA + "\n"
        if a[:3] == ("api", "-X", "POST"):
            run_id = self.dispatch_ids.pop(0)
            self.run(run_id, head=MOVED)
            return 0, json.dumps({"workflow_run_id": run_id, "run_url": "u", "html_url": "h"})
        if a[0] == "api" and a[1].endswith("/jobs"):
            run_id = int(a[1].split("/")[-2])
            tagged = run_id in self.tags
            return 0, json.dumps({"jobs": [{"id": run_id * 10, "name": "build", "conclusion": "success"},
                                           {"id": run_id * 10 + 1, "name": "publish", "conclusion": "success" if tagged else "skipped"}]})
        if a[0] == "api" and "/actions/runs/" in a[1]:
            run_id = int(a[1].rsplit("/", 1)[1])
            return (0, json.dumps(self.runs[run_id])) if run_id in self.runs else (1, "")
        if a[:2] == ("run", "view"):
            run_id = int(a[a.index("--job") + 1]) // 10
            return 0, f"publish\tPublish\t2026-11-13T18:59:39.1961713Z   RELEASE_TAG: {self.tags[run_id]}\n"
        if a[:2] == ("run", "list"):
            return 0, json.dumps(self.listed)
        return 1, ""

    def dispatched(self):
        return [c for c in self.calls if c[:3] == ("api", "-X", "POST")]


@pytest.fixture
def rel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(tmp_path / "flow"))
    monkeypatch.setattr(hzlib, "_gh_process", lambda *a, **_: subprocess.CompletedProcess(a, 1, "", ""))
    module = edge.fresh(edge.module_of("hz-release"))
    module.clock, module.fake = Clock(), FakeGh()
    monkeypatch.setattr(module, "time", module.clock)
    monkeypatch.setattr(module.hzlib, "sleep", module.clock.sleep)
    monkeypatch.setattr(module.hzlib, "gh", module.fake)
    module.said = []
    monkeypatch.setattr(module.hzlib, "say", lambda text, *a, **k: module.said.append(text))
    module.pypi = [False]
    monkeypatch.setattr(module, "pypi_has", lambda *a: module.pypi[0])
    (tmp_path / ".cache").mkdir()
    return module


def receipt(rel):
    return json.loads(rel.plugin_receipt().read_text())


def finish_after(rel, run_id, conclusion, tag):
    """The run ends after one poll; PyPI has the version exactly when the run published it."""
    def sleep(seconds):
        rel.clock.sleep(seconds)
        rel.fake.run(run_id, "completed", conclusion, tag, head=MOVED)
        rel.pypi[0] = conclusion == "success"
    rel.hzlib.sleep = sleep


def test_the_dispatch_records_its_run_id_and_the_head_the_run_took(rel):
    finish_after(rel, GOOGLE, "success", "heinzel-google/v0.9.4")
    assert rel.release_plugin("google", 3600) == 0
    data = receipt(rel)
    assert data["plugin-heinzel-google-0.9.4_run_id"] == GOOGLE
    assert data["plugin-heinzel-google-0.9.4_dispatch"]["sha"] == MOVED
    assert data["plugin-heinzel-google-0.9.4_dispatch"]["inputs"] == {"plugin": "heinzel-google", "publish": "true", "registry": "pypi"}
    [post] = rel.fake.dispatched()
    assert "inputs[plugin]=heinzel-google" in post and "return_run_details=true" in post
    assert rel.said[-1] == f"RELEASED heinzel-google 0.9.4, verified on PyPI (run {GOOGLE}, heinzel-google/v0.9.4)"


def test_a_resume_follows_only_the_recorded_run_never_the_newest_of_another_plugin(rel):
    finish_after(rel, GOOGLE, "failure", None)
    with pytest.raises(rel.Stop, match=rf"run {GOOGLE} ended failure"):
        rel.release_plugin("google", 3600)
    rel.fake.calls.clear()
    with pytest.raises(rel.Stop, match=rf"run {GOOGLE} ended failure .*--redispatch"):
        rel.release_plugin("google", 3600)
    assert not any(c[:2] == ("run", "list") for c in rel.fake.calls)  # no newest-runs window is looked at
    assert not any(str(TAILSCALE) in " ".join(c) for c in rel.fake.calls)
    assert not rel.fake.dispatched()


def test_a_time_only_stamp_adopts_no_run_and_dispatches_nothing(rel):
    rel.plugin_receipt().write_text(json.dumps({"plugin-heinzel-google-0.9.4": "2026-11-13T18:51:37Z"}))
    with pytest.raises(rel.Stop, match=r"only a dispatch time .* no other run is adopted"):
        rel.release_plugin("google", 3600)
    assert not rel.fake.dispatched() and not any(c[:2] == ("run", "list") for c in rel.fake.calls)


def test_success_needs_the_runs_release_tag_to_be_this_plugin_and_version(rel):
    finish_after(rel, GOOGLE, "success", "heinzel-tailscale/v0.9.7")
    with pytest.raises(rel.Stop, match=rf"recorded release run {GOOGLE} released heinzel-tailscale/v0\.9\.7, not heinzel-google/v0\.9\.4"):
        rel.release_plugin("google", 3600)
    assert not any(line.startswith("RELEASED") for line in rel.said)


def test_success_without_a_readable_release_tag_stops(rel):
    finish_after(rel, GOOGLE, "success", None)
    with pytest.raises(rel.Stop, match=r"released no readable RELEASE_TAG"):
        rel.release_plugin("google", 3600)


def test_a_failed_run_is_dispatched_again_only_with_redispatch(rel):
    finish_after(rel, GOOGLE, "failure", None)
    with pytest.raises(rel.Stop):
        rel.release_plugin("google", 3600)
    rel.fake.dispatch_ids.append(AGAIN)
    finish_after(rel, AGAIN, "success", "heinzel-google/v0.9.4")
    assert rel.release_plugin("google", 3600, redispatch=True) == 0
    data = receipt(rel)
    assert data["plugin-heinzel-google-0.9.4_run_id"] == AGAIN
    assert [r["run"] for r in data["plugin-heinzel-google-0.9.4_replaced"]] == [GOOGLE]
    assert len(rel.fake.dispatched()) == 2


def test_redispatch_refuses_while_the_recorded_run_is_active(rel):
    rel.fake.run(GOOGLE, head=MOVED)
    rel.plugin_receipt().write_text(json.dumps({
        "plugin-heinzel-google-0.9.4": "2026-11-13T18:51:37Z", "plugin-heinzel-google-0.9.4_run_id": GOOGLE,
        "plugin-heinzel-google-0.9.4_dispatch": {"repo": rel.PLUGINS, "workflow": "release.yml", "ref": "main", "sha": MOVED,
                                                 "event": "workflow_dispatch", "inputs": {}}}))
    with pytest.raises(rel.Stop, match=r"--redispatch refused: .* not completed"):
        rel.release_plugin("google", 3600, redispatch=True)
    assert not rel.fake.dispatched()


def test_a_time_only_stamp_dispatches_with_redispatch(rel):
    rel.plugin_receipt().write_text(json.dumps({"plugin-heinzel-google-0.9.4": "2026-11-13T18:51:37Z"}))
    finish_after(rel, GOOGLE, "success", "heinzel-google/v0.9.4")
    assert rel.main(["--plugin", "google", "--redispatch"]) == 0
    assert receipt(rel)["plugin-heinzel-google-0.9.4_run_id"] == GOOGLE


def test_redispatch_goes_only_with_plugin(rel):
    with pytest.raises(SystemExit) as stop:
        rel.main(["0.9.41", "--redispatch"])
    assert stop.value.code == 2


def test_a_dispatch_reply_without_a_run_id_adopts_no_run(rel):
    real = rel.fake.__call__

    def no_details(*a):
        if a[:3] == ("api", "-X", "POST"):
            rel.fake.calls.append(a)
            return 0, ""  # 204: no run details
        return real(*a)
    rel.hzlib.gh = no_details
    with pytest.raises(rel.Stop, match=r"GitHub named no run ID .* no run is adopted"):
        rel.release_plugin("google", 3600)
    data = receipt(rel)
    assert data["plugin-heinzel-google-0.9.4_run_id"] is None and data["plugin-heinzel-google-0.9.4_dispatch"]["sha"] == SHA
    with pytest.raises(rel.Stop, match=r"GitHub named no run ID"):  # a rerun still adopts the listed tailscale run never
        rel.release_plugin("google", 3600)
    assert not any(c[:2] == ("run", "list") for c in rel.fake.calls)
    rel.hzlib.gh = rel.fake
    finish_after(rel, GOOGLE, "success", "heinzel-google/v0.9.4")
    assert rel.release_plugin("google", 3600, redispatch=True) == 0
