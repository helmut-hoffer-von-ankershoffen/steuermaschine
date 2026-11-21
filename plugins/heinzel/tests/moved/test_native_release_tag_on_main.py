"""Tag-on-green-main (core ADR 0144, research/continuous-release-2026-11-08.md changes 2-3), hz-release part: behind the
flag HZ_RELEASE_TAG_ON_MAIN (default OFF, pinned per version), no freeze, the bump commit's first-attempt main run is
the candidate, the tag points at exactly that commit, and `previous release accepted` still holds PyPI."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from steuermaschine_heinzel.native.queue_lib import release_flow  # noqa: E402
from steuermaschine_heinzel import state as statedir

V, TAG = "1.2.4", "v1.2.4"
SHA1, SHA2 = "1" * 40, "2" * 40


class World:
    """GitHub, the queue and the release checkout as one fake; every poll advances it one tick."""

    def __init__(self):
        self.events = []
        self.prs = []  # {"number", "state", "headRefName", "title", "mergeCommit"}
        self.runs = {}  # sha -> list of run rows
        self.verdicts = {}  # sha -> evidence dict or Exception
        self.tags = {}
        self.ship_result = (0, "queued")
        self.branch = ""
        self.release_ok = True
        self.tag_runs = []
        self.previous_ok = True
        self.published = True
        self.merge_after_ship = True
        self.finish_runs = True
        self.ticks = 0

    def tick(self):
        for pr in self.prs:
            if pr["state"] == "OPEN" and pr.get("queued") and self.merge_after_ship:
                sha = SHA1 if pr["number"] == 11 else SHA2
                pr |= {"state": "MERGED", "mergeCommit": {"oid": sha}}
                self.runs.setdefault(sha, [{"status": "in_progress"}])
        if self.finish_runs:
            for rows in self.runs.values():
                for row in rows:
                    row["status"] = "completed"
        if self.events and self.events[-1][0] == "dispatch-tag" and self.events[-1][1] not in self.tags.values():
            self.tags[TAG] = self.events[-1][1]


@pytest.fixture
def rel(tool, home, monkeypatch):
    r = tool("hz-release")
    w = World()
    r.world = w
    def sleep(seconds):
        w.ticks += 1
        if w.ticks > 50:  # stands in for a step timeout without real waiting
            w.ticks = 0
            raise r.Stop("step: timed out")
        w.tick()
    monkeypatch.setattr(r.hzlib, "sleep", sleep)
    monkeypatch.setattr(r, "POLL", 0)
    monkeypatch.setattr(r, "next_version", lambda request: (V, "patch"))
    monkeypatch.setattr(r, "rejection_guard", lambda v: None)
    monkeypatch.setattr(r, "has_tag", lambda repo, tag: tag in w.tags)
    monkeypatch.setattr(r, "tag_commit", lambda tag: w.tags.get(tag, ""))
    monkeypatch.setattr(r, "main_candidate_gate", lambda v: True)
    monkeypatch.setattr(r, "main_tip_green", lambda: True)

    def release_script(work, bump, intro, extra=()):
        w.events.append(("release.sh", bump, tuple(extra)))
        if not w.release_ok:
            return type("Done", (), {"returncode": 1, "stderr": "error: boom"})()
        number = 11 if "--refresh" not in extra else 12
        w.branch = f"bump/{TAG}" if number == 11 else f"bump/{TAG}-refresh-{'a' * 12}"
        w.prs.append({"number": number, "state": "OPEN", "headRefName": w.branch, "title": "chore(release)",
                      "mergeCommit": None})
        return type("Done", (), {"returncode": 0, "stderr": ""})()
    monkeypatch.setattr(r, "run_release_script", release_script)
    monkeypatch.setattr(r, "checkout_branch", lambda work: w.branch)
    monkeypatch.setattr(r, "bump_prs", lambda tag: [dict(p) for p in w.prs])

    def simulated_open_bump_pr(version, bump_kind, intro, refresh):
        tag = f"v{version}"
        data = r.flow_data(version)
        current = release_flow.current_bump(data)
        if current and current.get("verdict") != "refused":
            return
        if current and not refresh:
            raise r.refused_stop(version, current)
        if refresh and not current:
            raise r.Stop(f"--refresh needs a refused candidate of {version} first")
        known = {b.get("branch") for b in data.get("bumps") or []}
        pending = [pr for pr in r.bump_prs(tag) if pr["headRefName"] not in known and pr.get("state") != "CLOSED"]
        if (refresh and pending and pending[0]["headRefName"].startswith(f"bump/{tag}-refresh-")) or (not refresh and pending):
            r.save_bump(version, {"branch": pending[0]["headRefName"], "pr": pending[0]["number"]})
            return
        extra = ("--on-main", "--refresh") if refresh else ("--on-main",)
        done = r.run_release_script("release-work", "auto" if refresh else bump_kind, intro, extra)
        if done.returncode != 0:
            raise r.Stop("release.sh failed: " + done.stderr)
        branch = r.checkout_branch("release-work")
        found = r.bump_pr(tag, branch)
        if not found or branch in known:
            raise r.Stop(f"release.sh left no new bump PR for {tag} (branch {branch!r})")
        r.save_bump(version, {"branch": branch, "pr": found["number"]})

    r.actual_open_bump_pr = r.open_bump_pr
    monkeypatch.setattr(r, "open_bump_pr", simulated_open_bump_pr)

    def ship(pr):
        w.events.append(("ship", pr))
        if w.ship_result[0] == 0:
            next(p for p in w.prs if p["number"] == pr)["queued"] = True
        return w.ship_result
    monkeypatch.setattr(r, "ship", ship)
    monkeypatch.setattr(r, "bump_runs", lambda sha: w.runs.get(sha))

    def verify_commit(sha, waiver=None):
        w.events.append(("verify", sha))
        result = w.verdicts.get(sha, {"run_id": 900, "job_count": 68, "head": sha})
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(r.release_candidate_policy, "verify_commit", verify_commit)

    def dispatch(sha):
        w.events.append(("dispatch-tag", sha))
        return True
    monkeypatch.setattr(r, "dispatch_tag_workflow", dispatch)
    monkeypatch.setattr(r, "runs", lambda repo, workflow, **kw: list(w.tag_runs))

    def published(tag):
        w.events.append(("published", tag))
        return w.published
    monkeypatch.setattr(r, "release_published", published)

    def previous(version):
        w.events.append(("previous accepted", version))
        if not w.previous_ok:
            raise r.Stop("previous release 1.2.3 NOT ACCEPTED (run 5): fix forward")
        return True
    monkeypatch.setattr(r, "previous_accepted", previous)
    monkeypatch.setattr(r, "compose_beside_pypi", lambda *a: w.events.append(("beside",)))
    monkeypatch.setattr(r, "pypi_step", lambda *a: w.events.append(("pypi",)))
    monkeypatch.setattr(r, "compose_step", lambda *a: w.events.append(("compose",)))
    monkeypatch.setattr(r, "acceptance_step", lambda *a: w.events.append(("acceptance",)) or 7)
    monkeypatch.setattr(r, "pinned_snapshot", lambda version: {"tag": TAG, "commit": w.tags.get(TAG)})
    monkeypatch.setattr(r, "verdict", lambda run: (True, []))
    monkeypatch.setattr(r, "publish_acceptance", lambda v, run, ok: w.events.append(("marker", ok)))
    return r


def flag(monkeypatch, on=True):
    if on:
        monkeypatch.setenv(release_flow.FLAG, "1")
    else:
        monkeypatch.delenv(release_flow.FLAG, raising=False)


def run(rel, *extra):
    return rel.main([V, "--intro", "Faster releases.", "--snapshot-acceptance", "--candidate-gate", "--timeout", "1", *extra])


def kinds(rel):
    return [e[0] for e in rel.world.events]


def test_flag_off_keeps_the_release_pr_flow_and_pins_it(rel, monkeypatch):
    flag(monkeypatch, on=False)
    seen = []
    monkeypatch.setattr(rel, "release_core_on_main", lambda *a, **kw: seen.append("on-main") or 0)
    monkeypatch.setattr(rel, "step", lambda name, *a, **kw: seen.append(name))
    monkeypatch.setattr(rel, "lift_freeze_at_tag", lambda *a: None)
    monkeypatch.setattr(rel, "release_snapshot_freeze", lambda: None)
    rel.release_core(V, "x", 60)
    assert "on-main" not in seen and "release PR merged" in seen and "freeze" in seen
    assert release_flow.read(V)["flow"] == release_flow.RELEASE_PR
    flag(monkeypatch)  # flipping the flag later never changes a running release
    assert rel.pick_flow(V, None) == release_flow.RELEASE_PR


def test_a_version_started_before_the_flag_stays_on_the_release_pr_flow(rel, monkeypatch):
    flag(monkeypatch)
    rel.write_state({"version": V})
    assert rel.pick_flow(V, None) == release_flow.RELEASE_PR
    assert rel.pick_flow("1.2.5", None) == release_flow.TAG_ON_MAIN
    rel.write_state({"version": "1.2.6", "freeze_token": "t"})  # an owned hold or pause: release-PR, no network first
    monkeypatch.setattr(rel, "has_tag", lambda *a: pytest.fail("network before the hold check"))
    assert rel.pick_flow("1.2.6", None) == release_flow.RELEASE_PR


def test_flag_file_switches_new_versions(rel, monkeypatch, tmp_path):
    flag(monkeypatch, on=False)
    assert not release_flow.flag_on()
    release_flow.flag_file().write_text("on")
    assert release_flow.flag_on() and rel.pick_flow(V, None) == release_flow.TAG_ON_MAIN


def test_happy_path_order_no_freeze_exact_tag_and_accepted_gate_before_the_tag(rel, monkeypatch):
    flag(monkeypatch)
    assert run(rel) == 0
    assert kinds(rel) == ["release.sh", "ship", "verify", "previous accepted", "dispatch-tag", "published", "beside",
                          "pypi", "compose", "acceptance", "marker"]  # compose starts beside the PyPI upload
    assert rel.world.events[0] == ("release.sh", "patch", ("--on-main",))
    assert rel.world.events[2] == ("verify", SHA1) and rel.world.events[4] == ("dispatch-tag", SHA1)
    assert rel.world.tags[TAG] == SHA1
    assert not rel.paths()[0].exists(), "no merge freeze sentinel"
    assert not [f.name for f in Path(rel.paths()[0]).parent.iterdir() if "freeze" in f.name or "hold" in f.name], \
        "no release-owned hold"
    tail = json.loads(rel.tail_receipt(V).read_text())
    assert tail["finished"] and tail["accepted"] and tail["candidate_ci"]["head"] == SHA1
    assert not rel.global_receipt().exists(), "handed off at the tag"
    bump = release_flow.read(V)["bumps"][-1]
    assert bump == {"branch": f"bump/{TAG}", "pr": 11, "queued": True, "sha": SHA1, "verdict": "ok", "run": 900}


def test_the_tag_waits_for_the_main_run_of_the_bump_commit(rel, monkeypatch):
    flag(monkeypatch)
    rel.world.finish_runs = False
    assert run(rel) == 1, "times out while the main run is still running"
    assert "verify" not in kinds(rel) and "dispatch-tag" not in kinds(rel)
    rel.world.finish_runs = True
    assert run(rel) == 0
    assert kinds(rel).count("release.sh") == 1 and kinds(rel).count("ship") == 1, "resume never re-cuts or re-ships"
    assert kinds(rel).count("verify") == 1


def test_refused_candidate_stops_before_the_tag_and_refresh_re_cuts_the_same_version(rel, monkeypatch, capsys):
    flag(monkeypatch)
    rel.world.verdicts[SHA1] = rel.release_candidate_policy.release_candidate.CandidateError("candidate job failed")
    assert run(rel) == 1
    err = capsys.readouterr().err
    assert "candidate of bump commit 111111111 refused: candidate job failed" in err and f"sm release resume {V} --refresh" in err
    assert "dispatch-tag" not in kinds(rel) and "pypi" not in kinds(rel) and TAG not in rel.world.tags
    assert run(rel) == 1, "a plain resume repeats the refusal"
    assert kinds(rel).count("verify") == 1, "a refusal is final for that commit: never re-judged"
    assert run(rel, "--refresh") == 0
    assert ("release.sh", "auto", ("--on-main", "--refresh")) in rel.world.events
    assert ("verify", SHA2) in rel.world.events and rel.world.tags[TAG] == SHA2
    bumps = release_flow.read(V)["bumps"]
    assert [b["verdict"] for b in bumps] == ["refused", "ok"] and bumps[1]["branch"].startswith(f"bump/{TAG}-refresh-")


def test_refresh_needs_a_refused_candidate_and_the_tag_on_main_flow(rel, monkeypatch, capsys):
    monkeypatch.setattr(rel, "open_bump_pr", rel.actual_open_bump_pr)
    flag(monkeypatch)
    rel.world.finish_runs = False
    assert run(rel, "--refresh") == 1
    assert "--refresh needs a refused candidate" in capsys.readouterr().err
    flag(monkeypatch, on=False)
    monkeypatch.setattr(rel, "next_version", lambda request: ("1.2.9", "patch"))
    assert rel.main(["1.2.9", "--refresh"]) == 1
    assert "only for a tag-on-green-main release" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        rel.main(["patch", "--refresh"])


def test_invalid_evidence_fails_closed(rel, monkeypatch):
    flag(monkeypatch)
    rel.world.verdicts[SHA1] = KeyError("run")
    assert run(rel) == 1
    assert release_flow.read(V)["bumps"][-1]["why"] == "invalid evidence (KeyError)"
    assert "dispatch-tag" not in kinds(rel)


def test_previous_release_not_accepted_holds_the_tag_of_n_plus_1(rel, monkeypatch, capsys):
    flag(monkeypatch)
    rel.world.previous_ok = False
    assert run(rel) == 1
    assert "NOT ACCEPTED" in capsys.readouterr().err
    assert TAG not in rel.world.tags, "N+1 is never tagged (= a public latest release) before N's verdict"
    assert "dispatch-tag" not in kinds(rel) and "published" not in kinds(rel) and "pypi" not in kinds(rel)
    assert rel.global_receipt().exists() and not rel.tail_receipt(V).exists(), "no handoff before the tag"
    rel.world.previous_ok = True
    assert run(rel) == 0, "resumes once N is accepted or absorbed"
    assert kinds(rel).count("verify") == 1 and kinds(rel).count("dispatch-tag") == 1
    assert kinds(rel)[-7:] == ["dispatch-tag", "published", "beside", "pypi", "compose", "acceptance", "marker"]


def test_the_pin_wins_over_a_hold_or_pause_shortcut(rel, monkeypatch):
    release_flow.write({"version": V, "flow": release_flow.TAG_ON_MAIN, "bumps": []})
    rel.write_state({"version": V, "freeze_token": "t", "hold_events": [{"action": "pause"}]})
    assert rel.pick_flow(V, None) == release_flow.TAG_ON_MAIN
    assert rel.pick_flow("patch", None) == release_flow.TAG_ON_MAIN


@pytest.mark.parametrize("action", ["--pause", "--prepare-only"])
def test_pause_and_prepare_only_refuse_a_tag_on_main_release(rel, monkeypatch, capsys, action):
    release_flow.write({"version": V, "flow": release_flow.TAG_ON_MAIN, "bumps": []})
    rel.write_state({"version": V})
    assert rel.main([V, action]) == 1
    err = capsys.readouterr().err
    assert f"{action} is for the release-PR flow" in err and f"sm release resume {V} --refresh" in err
    assert rel.state() == {"version": V}, "nothing changed"


def test_a_tag_that_points_elsewhere_stops(rel, monkeypatch, capsys):
    flag(monkeypatch)
    real = rel.dispatch_tag_workflow

    def wrong(sha):
        real(sha)
        rel.world.tags[TAG] = "9" * 40
        return True
    monkeypatch.setattr(rel, "dispatch_tag_workflow", wrong)
    rel.world.events.append(("noop", ""))
    assert run(rel) == 1
    assert "does not point at the verified bump commit" in capsys.readouterr().err
    assert "pypi" not in kinds(rel)


def test_a_tag_without_a_verified_bump_stops(rel, monkeypatch, capsys):
    flag(monkeypatch)
    release_flow.write({"version": V, "flow": release_flow.TAG_ON_MAIN, "bumps": []})
    rel.world.tags[TAG] = SHA1
    assert run(rel) == 1
    assert "exists without a verified bump commit" in capsys.readouterr().err


def test_a_failed_release_tag_run_stops(rel, monkeypatch, capsys):
    flag(monkeypatch)
    monkeypatch.setattr(rel, "dispatch_tag_workflow", lambda sha: rel.world.events.append(("dispatch-tag-x", sha)) or True)
    rel.world.tag_runs = [{"databaseId": 77, "status": "completed", "conclusion": "failure", "createdAt": "9999-01-01T00:00:00Z"}]
    assert run(rel) == 1
    assert "release-tag run 77 ended failure" in capsys.readouterr().err


def test_hz_ship_refusal_and_a_closed_bump_pr_stop(rel, monkeypatch, capsys):
    flag(monkeypatch)
    rel.world.ship_result = (2, "NEEDS_REVIEW 11")
    assert run(rel) == 1
    assert "did not queue the bump PR (exit 2): NEEDS_REVIEW 11" in capsys.readouterr().err
    rel.world.prs[0]["state"] = "CLOSED"
    rel.world.ship_result = (0, "queued")
    assert run(rel) == 1
    assert "was closed unmerged" in capsys.readouterr().err
    assert "verify" not in kinds(rel)


def test_release_sh_failure_stops_without_recording_a_bump(rel, monkeypatch, capsys):
    flag(monkeypatch)
    rel.world.release_ok = False
    assert run(rel) == 1
    assert "release.sh failed: error: boom" in capsys.readouterr().err
    assert release_flow.read(V)["bumps"] == []


def test_a_crash_after_release_sh_adopts_the_pushed_bump_pr(rel, monkeypatch):
    monkeypatch.setattr(rel, "open_bump_pr", rel.actual_open_bump_pr)
    flag(monkeypatch)
    rel.world.prs.append({"number": 11, "state": "OPEN", "headRefName": f"bump/{TAG}", "title": "x", "mergeCommit": None})
    assert run(rel) == 0
    assert "release.sh" not in kinds(rel)


def test_real_refresh_without_existing_pr_refuses_unscoped_creation(rel, monkeypatch):
    flag(monkeypatch)
    assert release_flow.choose(V, started_before=False) == release_flow.TAG_ON_MAIN
    rel.save_bump(V, {"branch": f"bump/{TAG}", "pr": 10, "verdict": "refused", "sha": SHA1})
    with pytest.raises(rel.Stop, match="blocked until its --open-pr push is release-scoped"):
        rel.actual_open_bump_pr(V, "patch", "intro", True)
    assert "release.sh" not in kinds(rel)


def test_corrupt_flow_record_fails_closed(rel, monkeypatch, capsys):
    flag(monkeypatch)
    release_flow.sidecar(V).parent.mkdir(parents=True, exist_ok=True)
    release_flow.sidecar(V).write_text("{")
    assert run(rel) == 1
    assert "unreadable" in capsys.readouterr().err
    assert not rel.world.events


def test_policy_refuses_until_the_reviewed_push_sources_are_pinned(tool):
    from steuermaschine_heinzel.native.release_lib import candidate_policy as policy

    if policy.TAG_ON_MAIN_SOURCES is not None:
        pytest.skip("pinned: covered by test_release_candidate_policy")
    with pytest.raises(policy.release_candidate.CandidateError, match="no independently reviewed"):
        policy.verify_commit(SHA1)


def test_queue_fix_forward_is_not_held_by_a_tag_on_main_release(tool, home, monkeypatch):
    queue = tool("hz-queue")
    monkeypatch.setattr(queue.hzlib, "merge_frozen", lambda *a: False)
    monkeypatch.setattr(queue.hzlib, "ghj", lambda *a, **kw: [])
    (statedir.root() / "release.json").write_text(json.dumps({"version": V}))
    release_flow.write({"version": V, "flow": release_flow.RELEASE_PR, "bumps": []})
    assert queue.release_active() == f"sm release resume {V} unfinished"
    release_flow.write({"version": V, "flow": release_flow.TAG_ON_MAIN, "bumps": []})
    assert queue.release_active() == ""
