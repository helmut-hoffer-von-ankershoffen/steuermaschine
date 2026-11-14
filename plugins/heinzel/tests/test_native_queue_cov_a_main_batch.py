"""queue_lib.main_batch: proof URLs, run and history validation, intent windows and admit, on a fake GitHub (a dict of endpoints)."""

import base64
import json
import time
from datetime import UTC, datetime
from types import SimpleNamespace

from steuermaschine_heinzel.native.queue_lib import main_batch as mb

BASE = "b" * 40
C1, C2 = "1" * 40, "2" * 40
PRS = [[11, C1], [12, C2]]
RUN = 99


def url(base=BASE, prs=PRS, run=RUN):
    return mb.proof_url(base, prs, run)


def status(target, state="success", creator=mb.PUBLISHER_ID, context=mb.CONTEXT, **more):
    return {"context": context, "state": state, "creator": {"id": creator}, "target_url": target, **more}


def run_row(**more):
    return {"id": RUN, "run_attempt": 1, "event": "push", "head_branch": "main", "head_sha": C2, "path": ".github/workflows/ci.yml",
            "repository": {"full_name": mb.REPOSITORY}, "status": "in_progress", "conclusion": None, **more}


def world(marker="present", u=None):
    u = u or url()
    jobs = []
    if marker != "absent":
        jobs = [{"name": f"Main batch full suite ({BASE})", "status": "completed" if marker == "present" else "in_progress",
                 "conclusion": "success" if marker == "present" else None}]
    return {
        f"commits/{C1}/statuses?per_page=100": [status(u)],
        f"commits/{C2}/statuses?per_page=100": [status(u)],
        f"actions/runs/{RUN}": run_row(),
        f"compare/{BASE}...{C2}": {"status": "ahead", "total_commits": 2, "commits": [{"sha": C1, "parents": [{"sha": BASE}]}, {"sha": C2, "parents": [{"sha": C1}]}]},
        "pulls/11": {"merged": True, "merge_commit_sha": C1, "base": {"ref": "main", "repo": {"full_name": mb.REPOSITORY}}},
        "pulls/12": {"merged": True, "merge_commit_sha": C2, "base": {"ref": "main", "repo": {"full_name": mb.REPOSITORY}}},
        f"actions/runs/{RUN}/attempts/1/jobs?per_page=100": {"total_count": len(jobs), "jobs": jobs},
    }


def getter(data):
    return lambda endpoint: data[endpoint]


def test_decode_roundtrip_and_malformed():
    good = url()
    assert mb.decode(good) == {"v": 1, "base": BASE, "prs": PRS, "run": RUN}
    assert mb.decode(None) is None and mb.decode("x" * 2000) is None
    assert mb.decode(good.replace("https://", "http://")) is None
    assert mb.decode(good.replace("github.com", "evil.com")) is None
    assert mb.decode(good + "#frag") is None
    assert mb.decode(good.split("?")[0]) is None
    assert mb.decode(good + "&x=1") is None
    assert mb.decode(good.split("=")[0] + "=%%%") is None
    assert mb.decode(good.split("=")[0] + "=" + "!" * 5) is None
    assert mb.decode(good.replace(f"runs/{RUN}?", "runs/5?")) is None
    bad_payload = base64.urlsafe_b64encode(b"not json").decode().rstrip("=")
    assert mb.decode(f"https://github.com/{mb.REPOSITORY}/actions/runs/{RUN}?batch={bad_payload}") is None
    for payload in ({"v": 2, "base": BASE, "prs": PRS, "run": RUN}, {"v": 1, "base": BASE, "prs": PRS, "run": RUN, "x": 1}):
        enc = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        assert mb.decode(f"https://github.com/{mb.REPOSITORY}/actions/runs/{RUN}?batch={enc}") is None
    # not canonical: extra padding-equivalent spelling is rejected by the roundtrip
    assert mb.decode(url(prs=[[11, C1], [12, BASE]])) is None  # base among members


def test_valid_members_and_payload_rules():
    assert mb._valid_members(PRS)
    for prs in ("x", [PRS[0]], [[i + 1, str(i) * 40] for i in range(6)], [[1], [2]], [[True, C1], [2, C2]], [[0, C1], [2, C2]], [[1, "zz"], [2, C2]],
                [[1, C1], [1, C2]], [[1, C1], [2, C1]], [[1, 5], [2, C2]], ["ab", "cd"]):
        assert not mb._valid_members(prs), prs
    assert not mb._valid_payload([], "/x")
    assert not mb._valid_payload({"v": 1, "base": 5, "prs": PRS, "run": RUN}, f"/{mb.REPOSITORY}/actions/runs/{RUN}")


def test_valid_run_rules():
    proof = mb.decode(url())
    assert mb.valid_run(run_row(), proof)
    assert mb.valid_run(run_row(status="completed", conclusion="success"), proof)
    assert not mb.valid_run(run_row(status="completed", conclusion="cancelled"), proof)
    assert not mb.valid_run(run_row(status="completed", conclusion=None), proof)
    assert not mb.valid_run(run_row(status="in_progress", conclusion="success"), proof)
    assert not mb.valid_run(run_row(run_attempt=2), proof)  # an older Core: attempt 1 only, as Core's main_batch.py
    assert mb.valid_run(run_row(run_attempt=2), proof, reruns=True)  # merge tier (Core ADR 0149): a rerun may complete it
    assert not mb.valid_run(run_row(run_attempt=0), proof, reruns=True)
    for more in ({"id": 5}, {"run_attempt": 0}, {"event": "pull_request"}, {"head_branch": "x"}, {"head_sha": C1}, {"path": "other"}, {"repository": {}}, {"status": "weird"}):
        assert not mb.valid_run(run_row(**more), proof), more
    assert not mb.valid_run("x", proof)


def test_attested_and_intent_url():
    target = url()
    assert mb._attested(status(target)) and mb._attested(status(target), target)
    assert not mb._attested(status(target), "other") and not mb._attested(status(target, state="pending"))
    assert not mb._attested(status(target, creator=1)) and not mb._attested(status(target, context="x")) and not mb._attested({"context": mb.CONTEXT})
    assert mb.intent_url(BASE, C1) == f"https://github.com/{mb.REPOSITORY}/compare/{BASE}...{C1}"


def iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_intent_remaining():
    now = 1_800_000_000.0
    pending = lambda age, **kw: status(mb.intent_url(BASE, C2), state="pending", created_at=iso(now - age), **kw)  # noqa: E731
    assert mb.intent_remaining(C2, pending(30), now) == 150
    assert mb.intent_remaining(C2, pending(500), now) == 0
    assert mb.intent_remaining(C2, pending(-5), now) == 0
    assert mb.intent_remaining(C2, "x", now) == 0
    assert mb.intent_remaining(C2, status(mb.intent_url(BASE, C2)), now) == 0
    assert mb.intent_remaining(C2, status("https://github.com/other", state="pending", created_at=iso(now)), now) == 0
    assert mb.intent_remaining(C2, status(mb.intent_url(C2, C2), state="pending", created_at=iso(now)), now) == 0
    assert mb.intent_remaining(C2, status(mb.intent_url(BASE, C2), state="pending"), now) == 0
    assert mb.intent_remaining(C2, status(mb.intent_url(BASE, C2), state="pending", created_at="2026-01-01"), now) == 0
    assert mb.intent_remaining(C2, status(mb.intent_url(BASE, C2), state="pending", created_at="garbageZ"), now) == 0


def test_valid_history_and_failures():
    proof = mb.decode(url())
    assert mb._valid_history(proof, getter(world()))
    for key, patch in ((f"compare/{BASE}...{C2}", {"status": "diverged", "total_commits": 2, "commits": []}),
                       (f"compare/{BASE}...{C2}", {"status": "ahead", "total_commits": 3, "commits": []}),
                       (f"compare/{BASE}...{C2}", {"status": "ahead", "total_commits": 2, "commits": [{"sha": C2}, {"sha": C1}]}),
                       (f"compare/{BASE}...{C2}", {"status": "ahead", "total_commits": 2, "commits": [{"sha": C1, "parents": []}, {"sha": C2, "parents": [{"sha": C1}]}]}),
                       ("pulls/11", {"merged": False}), ("pulls/11", {"merged": True, "merge_commit_sha": "x"}),
                       ("pulls/12", {"merged": True, "merge_commit_sha": C2, "base": {"ref": "dev", "repo": {"full_name": mb.REPOSITORY}}}),
                       ("pulls/12", {"merged": True, "merge_commit_sha": C2, "base": {"ref": "main", "repo": {"full_name": "o/x"}}})):
        data = world()
        data[key] = patch
        assert not mb._valid_history(proof, getter(data)), (key, patch)


def test_latest_status_and_job_pages():
    assert mb._latest_status(C1, lambda e: None) is None
    assert mb._latest_status(C1, lambda e: [{"context": "other"}, {"context": "HZ/Main-Batch", "n": 1}, {"context": mb.CONTEXT, "n": 2}])["n"] == 1
    assert mb._latest_status(C1, lambda e: [{"context": "other"}]) is None
    ok = {"name": "x", "status": "completed", "conclusion": "success"}
    assert mb._valid_job_page({"total_count": 1, "jobs": [ok]})
    assert mb._valid_job_page({"total_count": 1, "jobs": [{"name": "x", "status": "queued", "conclusion": None}]})
    for page in (None, {"total_count": "1", "jobs": []}, {"total_count": 1, "jobs": "x"}, {"total_count": 2, "jobs": [ok]}, {"total_count": 1, "jobs": ["x"]},
                 {"total_count": 1, "jobs": [{**ok, "name": " "}]}, {"total_count": 1, "jobs": [{**ok, "name": 5}]}, {"total_count": 1, "jobs": [{**ok, "status": "odd"}]},
                 {"total_count": 1, "jobs": [{**ok, "conclusion": "odd"}]}, {"total_count": 1, "jobs": [{**ok, "status": "queued"}]}):
        assert not mb._valid_job_page(page), page


def test_marker_states():
    proof = mb.decode(url())
    key = f"actions/runs/{RUN}/attempts/1/jobs?per_page=100"
    assert mb._marker_state(proof, getter(world("present"))) == "present"
    assert mb._marker_state(proof, getter(world("running"))) == "waiting"
    assert mb._marker_state(proof, getter(world("absent"))) == "waiting"
    data = world()
    data[key] = {"total_count": 1, "jobs": [{"name": f"Main batch full suite ({BASE})", "status": "completed", "conclusion": "failure"}]}
    assert mb._marker_state(proof, getter(data)) == "invalid"
    marker = {"name": f"Main batch full suite ({BASE})", "status": "completed", "conclusion": "success"}
    data[key] = {"total_count": 2, "jobs": [marker, {**marker, "name": f"Main batch focused scope ({BASE})"}]}
    assert mb._marker_state(proof, getter(data)) == "invalid"
    data[key] = None
    assert mb._marker_state(proof, getter(data)) == "invalid"


def test_identity_and_proof_for():
    data = world()
    assert mb._identity_for(C1, getter(data))["run"] == RUN
    assert mb.proof_for(C1, getter(data))["run"] == RUN and mb.proof_for(C2, getter(world("absent")))["run"] == RUN
    assert mb.proof_for(C1, getter(world("absent"))) is None
    other = "9" * 40
    data[f"commits/{other}/statuses?per_page=100"] = [status(url())]
    assert mb._identity_for(other, getter(data)) is None
    data[f"commits/{other}/statuses?per_page=100"] = []
    assert mb._identity_for(other, getter(data)) is None
    data = world()
    data[f"commits/{C1}/statuses?per_page=100"] = [status(url(), creator=1)]
    assert mb._identity_for(C1, getter(data)) is None
    data = world()
    data[f"commits/{C1}/statuses?per_page=100"] = [status("https://github.com/x")]
    assert mb._identity_for(C1, getter(data)) is None
    data = world()
    data[f"commits/{C2}/statuses?per_page=100"] = [status("other")]
    assert mb._identity_for(C1, getter(data)) is None
    data = world()
    data[f"actions/runs/{RUN}"] = run_row(run_attempt=0)
    assert mb._identity_for(C1, getter(data)) is None
    data = world()
    data["pulls/11"] = {"merged": False}
    assert mb._identity_for(C1, getter(data)) is None


def test_inspect_admission_branches():
    now = time.time()
    assert mb._inspect_admission(C1, getter({f"commits/{C1}/statuses?per_page=100": []}), now) == (None, None)
    pending = status(mb.intent_url(BASE, C1), state="pending", created_at=iso(now - 10))
    result, authority = mb._inspect_admission(C1, getter({f"commits/{C1}/statuses?per_page=100": [pending]}), now)
    assert result is None and 160 < authority < 175
    assert mb._inspect_admission(C1, getter({f"commits/{C1}/statuses?per_page=100": [status("x", state="failure")]}), now) == (mb._full_run(), 0)
    data = world()
    assert mb._inspect_admission(C1, getter(data), now) == ({"full": False, "base": BASE, "run": RUN}, 0)
    assert mb._inspect_admission(C2, getter(data), now) == ({"full": True, "base": BASE, "run": RUN}, 0)
    assert mb._inspect_admission(C1, getter(world("running")), now) == (None, mb.INTENT_SECONDS)
    data = world()
    data["pulls/11"] = {"merged": False}
    assert mb._inspect_admission(C1, getter(data), now) == (mb._full_run(), 0)
    data = world()
    data[f"actions/runs/{RUN}/attempts/1/jobs?per_page=100"] = None
    assert mb._inspect_admission(C1, getter(data), now) == (mb._full_run(), 0)


def test_admit_paths(monkeypatch):
    clock = SimpleNamespace(t=0.0)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock.t += s

    kw = {"clock": lambda: clock.t, "sleep": sleep}
    assert mb.admit(C1, getter(world()), **kw) == {"full": False, "base": BASE, "run": RUN}

    def boom(endpoint):
        raise OSError("net")

    assert mb.admit(C1, boom, **kw) == mb._full_run()
    # no status at all: waits out the short grace, then full
    assert mb.admit(C1, lambda e: [], **kw) == mb._full_run() and sleeps and sum(sleeps) <= mb.GRACE_SECONDS
    # a marker that never arrives: authority waits, bounded by `seconds`
    clock.t = 0.0
    sleeps.clear()
    assert mb.admit(C1, getter(world("running")), seconds=20, **kw) == mb._full_run() and 15 <= sum(sleeps) <= 20
    # intent that is revoked after the first look never inherits the longer wait
    now = time.time()
    answers = iter([[status(mb.intent_url(BASE, C1), state="pending", created_at=iso(now))], []])
    clock.t = 0.0
    assert mb.admit(C1, lambda e: next(answers, []), **kw) == mb._full_run()


def test_main_outside_and_inside_a_push(monkeypatch, tmp_path):
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    for name in ("GITHUB_REPOSITORY", "GITHUB_EVENT_NAME", "GITHUB_REF", "GITHUB_RUN_ATTEMPT"):
        monkeypatch.delenv(name, raising=False)
    mb.main()
    assert out.read_text().splitlines() == ["full=true", "base=", "run="]
    monkeypatch.setenv("GITHUB_REPOSITORY", mb.REPOSITORY)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_SHA", C1)
    data = world()
    calls = []

    def fake_run(argv, **kw):
        endpoint = argv[2].split(f"repos/{mb.REPOSITORY}/")[1]
        calls.append(kw["timeout"])
        return SimpleNamespace(stdout=json.dumps(data[endpoint]))

    monkeypatch.setattr(mb.edge, "run", fake_run)
    out.write_text("")
    mb.main()
    assert out.read_text().splitlines() == ["full=false", f"base={BASE}", f"run={RUN}"] and calls and max(calls) <= 2
