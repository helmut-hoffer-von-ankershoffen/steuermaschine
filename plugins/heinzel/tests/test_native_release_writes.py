"""`sm release resume | plugin | drop` through the command tree, and their parity with the old step runner.

fixtures/release/transcripts/*.json: what the OLD script did in six scenarios, recorded 2026-11-12 23:24 CET with
`uv run python plugins/heinzel/tests/record_release_transcripts.py COPY COPY_WITH_DROP_PATCH` on copies of the old
tools (throwaway HOME, scripted gh and PyPI; nothing real was touched). The parity tests replay each scenario on the
native command with the same scripted `gh` (`steuermaschine.github.testing.FakeGh`) and compare the exit code, every
line, every `gh` call in order, and every state file afterwards.
fixtures/release/receipt-0.9.40-pretag.json, tails/: real receipts of 2026-11-12, copied with `cp` (read only)."""

import json
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import state as statedir
from steuermaschine_heinzel.native import release as area
from steuermaschine_heinzel.native.release_lib.names import translate
from steuermaschine_heinzel.plugin import plugin

REAL = Path(__file__).parent / "fixtures" / "release"
TRANSCRIPTS = REAL / "transcripts"
SEQ = "__in_order__"
# scenario -> the native command line that equals the recorded old argument list
NATIVE = {
    "resume-main-red": ["release", "resume", "0.9.41", "--timeout", "1"],
    "absorb": ["release", "resume", "0.9.40", "--absorb", "--reason", "fixed in 0.9.41 by #812"],
    "absorb-accepted-refused": ["release", "resume", "0.9.40", "--absorb", "--reason", "fixed in 0.9.41 by #812"],
    "drop": ["release", "drop", "0.9.41", "--reason", "candidate red: Coverage, fix in #812"],
    "drop-close-pr": ["release", "drop", "0.9.41", "--reason", "candidate red: Coverage, fix in #812", "--close-pr"],
    "drop-published-refused": ["release", "drop", "0.9.41", "--reason", "candidate red: Coverage, fix in #812"],
}
STAMP = re.compile(r"20\d{6}T\d{6}Z|20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def make(tmp_path, monkeypatch, gh, reviewed=True):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("STEUERMASCHINE_HOME", str(tmp_path / ".local" / "state" / "steuermaschine"))  # the state directory under this HOME
    monkeypatch.setenv("HZ_RELEASE_FLOW_DIR", str(statedir.root() / "flow"))
    monkeypatch.delenv("HZ_RELEASE_TAG_ON_MAIN", raising=False)
    (tmp_path / ".cache").mkdir(exist_ok=True)
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"release_dir": str(tmp_path / "work"), "release_core_checkout": str(tmp_path / "core"), "queue_records_dir": str(tmp_path / "records")}}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed), stack, settings, _providers=fake_providers(commands=gh))


def test_compose_replacement_rejects_invalid_version_before_access(tmp_path, monkeypatch):
    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    out = invoke(rt, ["release", "replace-compose", "../escape", "--failed-run", "123", "--failed-attempt", "1",
                      "--reviewed-head", "a" * 40, "--core-source", "b" * 40, "--receipt-sha256", "c" * 64,
                      "--reason", "review: fixture"])
    assert out.exit_code == 1 and "exact release version" in out.stderr
    assert gh.calls == []


@pytest.mark.parametrize("exists", [False, True])
def test_compose_replacement_command_routes_and_releases_tail_lock(tmp_path, monkeypatch, exists):
    from steuermaschine_heinzel.native.release_lib import compose_harness

    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    path = tmp_path / "tail.json"
    if exists:
        path.write_text("{}")
    calls = []
    class Stop(Exception):
        pass
    module = SimpleNamespace(Stop=Stop, release_rejection=SimpleNamespace(InvalidRejection=ValueError),
                             tail_receipt=lambda version: path, route_to_tail=lambda version: calls.append("lock"),
                             rejection_guard=lambda version: calls.append("guard"),
                             free_tail_lock=lambda: calls.append("unlock"), RECEIPT_OVERRIDE=None)
    monkeypatch.setattr(area, "runner", lambda: module)
    monkeypatch.setattr(compose_harness, "replace", lambda *args: calls.append("replace"))
    out = invoke(rt, ["release", "replace-compose", "0.9.48", "--failed-run", "123", "--failed-attempt", "1",
                      "--reviewed-head", "a" * 40, "--core-source", "b" * 40, "--receipt-sha256", "c" * 64,
                      "--reason", "review: fixture"])
    if exists:
        assert out.exit_code == 0 and "replacement requested" in out.stdout
        assert calls == ["lock", "guard", "replace", "unlock"]
    else:
        assert out.exit_code == 1 and "tail receipt is absent" in out.stderr
        assert calls == ["unlock"]
    assert gh.calls == []


def capacity_args(version="0.9.49", mode="prepare", *extra):
    return ["release", "acceptance-capacity", version, "--mode", mode, "--failed-run", "123",
            "--failed-attempt", "1", "--receipt-sha256", "a" * 64, *extra]


@pytest.mark.parametrize(("version", "mode", "extra"), [
    ("patch", "prepare", []),
    ("0.9.49", "retry", []),
    ("0.9.49", "request", []),
    ("0.9.49", "prepare", ["--audit-sha256", "b" * 64]),
    ("0.9.49", "request", ["--audit-sha256", "short"]),
])
def test_capacity_cli_refuses_invalid_options_before_tail_access(tmp_path, monkeypatch, version, mode, extra):
    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    out = invoke(rt, capacity_args(version, mode, *extra))
    assert out.exit_code == 1 and "exact release version" in out.stderr
    assert not (statedir.root() / "release").exists()
    assert gh.calls == []


@pytest.mark.parametrize("outcome", ["prepare", "request", "capacity-refusal", "unavailable"])
def test_capacity_cli_routes_exact_evidence_and_releases_tail(tmp_path, monkeypatch, outcome):
    from steuermaschine_heinzel.native.release_lib import acceptance_capacity
    from steuermaschine_heinzel.native.release_lib.candidate_capacity import InvalidCapacity

    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    tail = statedir.root() / "release/v0.9.49.json"
    tail.parent.mkdir(parents=True)
    tail.write_text('{"accepted": false}')
    before = tail.read_bytes()
    bound = []
    mode = "request" if outcome == "request" else "prepare"
    audit = "b" * 64
    marker = "release-" + "c" * 32

    def recover(module, version, run_id, attempt, receipt_sha, audit_sha):
        bound.append(module)
        assert (version, run_id, attempt, receipt_sha, audit_sha) == (
            "0.9.49", 123, 1, "a" * 64, audit if mode == "request" else None,
        )
        assert module.paths()[1] == tail and module.TAIL_LOCK[0] == version
        if outcome == "capacity-refusal":
            raise InvalidCapacity("Mac job executed")
        if outcome == "unavailable":
            raise OSError("private provider detail")
        return {"audit_sha256": audit, "replacement_marker": marker if mode == "request" else None,
                "head": "d" * 40, "mac_job": 456, "linux_artifact": 789}

    monkeypatch.setattr(acceptance_capacity, "recover", recover)
    args = capacity_args("0.9.49", mode, *(["--audit-sha256", audit] if mode == "request" else []))
    out = invoke(rt, args)
    if outcome == "prepare":
        assert out.exit_code == 0 and f"Prepared capacity audit {audit}" in out.stdout
        assert "Mac job 456" in out.stdout and "Linux receipt 789" in out.stdout
    elif outcome == "request":
        assert out.exit_code == 0 and marker in out.stdout and "Original rejection 123 preserved" in out.stdout
    elif outcome == "capacity-refusal":
        assert out.exit_code == 1 and "Mac job executed" in out.stderr
    else:
        assert out.exit_code == 1 and "evidence could not be authenticated" in out.stderr
        assert "private provider detail" not in out.stderr
    assert len(bound) == 1 and bound[0].TAIL_LOCK is None and bound[0].RECEIPT_OVERRIDE is None
    assert tail.read_bytes() == before and gh.calls == []


def test_capacity_cli_missing_receipt_and_lock_contention_refuse_before_remote_access(tmp_path, monkeypatch):
    import fcntl

    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    absent = invoke(rt, capacity_args())
    assert absent.exit_code == 1 and "tail receipt is absent" in absent.stderr
    tail = statedir.root() / "release/v0.9.49.json"
    tail.parent.mkdir(parents=True, exist_ok=True)
    tail.write_text('{"accepted": false}')
    with tail.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = invoke(rt, capacity_args())
    assert busy.exit_code == 1 and "tail of v0.9.49 is running" in busy.stderr
    assert gh.calls == []


def pypi(monkeypatch, rules: dict):
    class Reply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return b"{}"

    def urlopen(request, timeout=None, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        status = next((code for part, code in rules.items() if part in url), 404)
        if status != 200:
            raise urllib.error.HTTPError(url, status, "scripted", {}, None)
        return Reply()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


def files(cache: Path) -> dict:
    out = {}
    for path in sorted(p for p in cache.rglob("*") if p.is_file()):
        name, text = str(path.relative_to(cache)), path.read_text()
        try:
            out[name] = json.loads(text) if name.endswith(".json") else text
        except ValueError:
            out[name] = text
    return out


def renamed(state: dict) -> dict:
    """A recorded state (paths in ~/.cache, old names) as the state directory holds it: the old prefix goes."""
    return {path.removeprefix("hz-"): content for path, content in state.items()}


def timeless(value):
    """The same data without its moments in time (receipt stamps, the archive's file name)."""
    if isinstance(value, dict):
        return {STAMP.sub("TIME", key): timeless(item) for key, item in value.items()}
    if isinstance(value, list):
        return [timeless(item) for item in value]
    return STAMP.sub("TIME", value) if isinstance(value, str) else value


@pytest.mark.parametrize("name", sorted(NATIVE))
def test_native_does_what_the_old_step_runner_did(name, tmp_path, monkeypatch):
    old = json.loads((TRANSCRIPTS / f"{name}.json").read_text())
    gh = FakeGh()
    for needle, answer in old["gh_rules"].items():
        gh.on(needle, *(answer[SEQ] if isinstance(answer, dict) and SEQ in answer else [answer]))
    rt = make(tmp_path, monkeypatch, gh)
    cache = statedir.root()  # the transcripts were recorded in ~/.cache under the old names: the same files, new names
    for path, content in renamed(old["state_before"]).items():
        (cache / path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        (cache / path).write_text(content if isinstance(content, str) else json.dumps(content))
    pypi(monkeypatch, old["pypi"])

    out = invoke(rt, ["--format", "json", *NATIVE[name]])
    doc = json.loads(out.stdout.replace(str(cache) + "/", "~/.cache/hz-"))  # the recorded lines name the old paths

    assert out.exit_code == old["exit"] == doc["exit"], out
    assert [call[1:] for call in gh.calls] == old["gh_calls"] and all(call[0] == "gh" for call in gh.calls)  # the same reads and writes, in the same order
    was_stopped = next((line.removeprefix("STOPPED: ") for line in old["stderr"] if line.startswith("STOPPED: ")), "")
    assert timeless(doc["lines"]) == timeless([translate(line) for line in old["stdout"] + [e for e in old["stderr"] if not e.startswith("STOPPED: ")]])
    assert doc["stopped"] == translate(was_stopped)
    assert timeless(files(cache)) == timeless(renamed(old["state_after"]))  # receipts, holds, locks, the freeze file, the archive: byte-equal data
    progress = [line for line in out.stderr.replace(str(cache) + "/", "~/.cache/hz-").splitlines()]
    assert timeless(progress) == timeless([translate(line) for line in old["stdout"]])  # every step is shown as it happens
    assert "hz-" not in out.stdout.replace(".cache/hz-release", "") and "hz-" not in "".join(doc["lines"]).replace(".cache/hz-release", "")


def test_the_transcripts_cover_a_freeze_a_receipt_and_two_github_writes():
    seen = {name: json.loads((TRANSCRIPTS / f"{name}.json").read_text()) for name in NATIVE}
    assert "hz-merge-freeze" in seen["resume-main-red"]["state_after"] and seen["resume-main-red"]["state_after"]["hz-release.json"] == {"version": "0.9.41"}
    assert seen["absorb"]["state_after"]["hz-release/v0.9.40.json"]["absorbed_by"] == "0.9.41"
    writes = [" ".join(call[:4]) for call in seen["drop-close-pr"]["gh_calls"] if call[0] == "pr" or "--method" in call]
    assert writes == ["pr close 812 -R", "api --method DELETE repos/helmut-hoffer-von-ankershoffen/heinzel/git/refs/heads/release/v0.9.41"]
    assert "hz-release-holds/" + "c" * 32 in seen["drop-close-pr"]["state_after"] and "hz-release-holds/" + "b" * 32 not in seen["drop-close-pr"]["state_after"]


# ----------------------------------------------------------------------------- the commands themselves
def test_the_release_writers_reach_the_command_module_the_release_library_and_the_queue_library():
    from steuermaschine.reviewgraph import reach
    from steuermaschine_heinzel import native

    found = reach(c for c in native.commands() if getattr(c, "path", "").startswith(("release resume", "release drop", "release plugin")))
    assert len(found) >= 3
    for unit in found:
        names = {name.removeprefix("steuermaschine_heinzel/native/") for name in unit.files}
        assert {"release.py", "release_lib/runner.py", "release_lib/candidate_policy.py", "queue_lib/core_policy.py", "release_lib/moved_map.py", "queue_lib/hzlib.py",
                "queue_lib/merge_train.py", "queue_lib/release_candidate.py"} <= names, unit.path
        assert not any(name.startswith("release_read") for name in names), unit.path


def test_the_policy_reader_reaches_every_release_writer_and_no_copy_of_core_is_kept():
    """The pins are Core's reviewed release policy at the checked commit: the plugin keeps no copy of Core's files, and
    a change to the reader of that policy (queue_lib/core_policy.py) reaches every release write command."""
    from steuermaschine.reviewgraph import reach, reached_by
    from steuermaschine_heinzel import native
    from steuermaschine_heinzel.native.queue_lib import core_policy

    lib = Path(core_policy.__file__).resolve().parent.parent / "release_lib"
    assert not (lib / "approved_sources").exists() and not (lib / "approved.py").exists()
    wanted = {"release resume", "release plugin", "release drop"}  # not the admission route (release admit ...): its own unit
    found = reach(c for c in native.commands() if getattr(c, "writes", False) and c.path in wanted)
    assert {unit.path for unit in found} == wanted
    reader = "plugins/heinzel/src/steuermaschine_heinzel/native/queue_lib/core_policy.py"
    assert set(reached_by(found, [reader])) == wanted


def typed(rt, monkeypatch, *args):
    seen = []
    monkeypatch.setattr(area, "run", lambda module, argv: seen.append(argv) or (0, [], []))
    out = invoke(rt, ["--format", "json", "release", *args])
    assert out.exit_code == 0, out
    return seen[0], json.loads(out.stdout)


def test_every_typed_option_becomes_the_step_runners_argument(tmp_path, monkeypatch):
    rt = make(tmp_path, monkeypatch, FakeGh())
    argv, doc = typed(rt, monkeypatch, "resume", "0.9.41", "--intro", "Faster start", "--snapshot-acceptance", "--candidate-gate", "--timeout", "90", "--wait-previous", "30")
    assert argv == ["0.9.41", "--snapshot-acceptance", "--candidate-gate", "--intro", "Faster start", "--timeout", "90", "--wait-previous", "30"] and doc["action"] == "resume"
    assert typed(rt, monkeypatch, "resume", "patch")[0] == ["patch"]  # nothing is added that was not typed
    assert typed(rt, monkeypatch, "resume", "0.9.41", "--pause") == (["0.9.41", "--pause"], doc | {"action": "pause"})
    assert typed(rt, monkeypatch, "resume", "0.9.41", "--prepare-only")[1]["action"] == "prepare-only"
    assert typed(rt, monkeypatch, "resume", "0.9.41", "--refresh")[0] == ["0.9.41", "--refresh"]
    assert typed(rt, monkeypatch, "resume", "migrate-legacy")[1]["action"] == "migrate-legacy"
    assert typed(rt, monkeypatch, "resume", "0.9.40", "--absorb", "--reason", "review: infra only")[0] == ["--absorb", "0.9.40", "--reason", "review: infra only"]
    assert typed(rt, monkeypatch, "resume", "0.9.41", "--acceptance-ref", "main", "--replace-acceptance-run", "77", "--replace-reason", "runner died")[0] == [
        "0.9.41", "--acceptance-ref", "main", "--replace-acceptance-run", "77", "--replace-reason", "runner died"]
    assert typed(rt, monkeypatch, "resume", "0.9.41", "--waive-job", "Coverage=abc", "--owner-quote", "waive coverage once")[0] == [
        "0.9.41", "--waive-job", "Coverage=abc", "--owner-quote", "waive coverage once"]
    assert typed(rt, monkeypatch, "resume", "0.9.41", "--use-candidate-composite", "88")[0] == ["0.9.41", "--use-candidate-composite", "88"]
    argv, doc = typed(rt, monkeypatch, "resume", "0.9.41", "--candidate-capacity", "request", "--candidate-run", "5", "--expected-audit-sha256", "a", "--expected-receipt-sha256", "b",
                      "--expected-freeze-token", "c")
    assert argv == ["0.9.41", "--candidate-capacity", "request", "--candidate-run", "5", "--expected-audit-sha256", "a", "--expected-receipt-sha256", "b",
                    "--expected-freeze-token", "c"] and doc["action"] == "candidate-capacity"
    argv, doc = typed(rt, monkeypatch, "resume", "0.9.41", "--reject-release", "--failed-compose-run", "9", "--failed-compose-attempt", "1", "--expected-receipt-sha256", "b",
                      "--expected-freeze-token", "c", "--expected-compose-head", "d", "--expected-core-source", "e", "--reason", "compose failed")
    assert argv == ["0.9.41", "--reject-release", "--reason", "compose failed", "--expected-receipt-sha256", "b", "--expected-freeze-token", "c", "--failed-compose-run", "9",
                    "--failed-compose-attempt", "1", "--expected-compose-head", "d", "--expected-core-source", "e"] and doc["action"] == "reject"
    assert typed(rt, monkeypatch, "plugin", "audio", "--timeout", "5") == (["--plugin", "audio", "--timeout", "5"], doc | {"action": "plugin", "version": ""})
    assert typed(rt, monkeypatch, "drop", "0.9.41", "--reason", "red", "--close-pr")[0] == ["0.9.41", "--drop-candidate", "--reason", "red", "--close-pr"]


def test_a_combination_the_step_runner_refuses_is_a_refusal_with_exit_2_and_nothing_written(tmp_path, monkeypatch):
    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    for args, why in ((["resume", "patch", "--pause"], "--pause requires an exact core version"), (["resume", "0.9.41", "--absorb"], "--absorb needs --reason"),
                      (["resume", "0.9.41", "--replace-reason", "x"], "--replace-reason requires --replace-acceptance-run"),
                      (["resume", "0.9.41", "--candidate-run", "5"], "candidate audit identity requires --candidate-capacity"),
                      (["drop", "patch", "--reason", "x"], "needs an exact core version X.Y.Z")):
        out = invoke(rt, ["release", *args])
        assert out.exit_code == 2 and why in out.stderr, (args, out)
    assert gh.calls == [] and list((tmp_path / ".cache").iterdir()) == []


def test_resume_of_a_real_finished_tail_stops_before_any_github_call_when_another_command_holds_its_lock(tmp_path, monkeypatch):
    import fcntl

    gh = FakeGh()
    rt = make(tmp_path, monkeypatch, gh)
    shutil.copytree(REAL / "tails", statedir.root() / "release")
    before = (statedir.root() / "release/v0.9.39.json").read_bytes()
    with (statedir.root() / "release/v0.9.39.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        out = invoke(rt, ["release", "resume", "0.9.39", "--snapshot-acceptance"])
    assert out.exit_code == 1 and out.stdout.strip().startswith("STOPPED: tail of v0.9.39 is running. Run the same command again")
    assert gh.calls == [] and (statedir.root() / "release/v0.9.39.json").read_bytes() == before
    out = invoke(rt, ["--format", "json", "release", "resume", "0.9.39", "--absorb", "--reason", "fixed in 0.9.40 by #800"])
    doc = area.ReleaseRun.model_validate(json.loads(out.stdout))  # the real receipt of an ACCEPTED release: nothing to absorb
    assert doc.exit == 1 and doc.stopped.startswith("release 0.9.39 is ACCEPTED: nothing to absorb") and doc.stands == "tail" and doc.finished and doc.accepted is True
    assert doc.acceptance_run == 43158281029 and (statedir.root() / "release/v0.9.39.json").read_bytes() == before


def test_drop_of_the_real_pretag_receipt_in_text_and_json(tmp_path, monkeypatch):
    def scripted():
        gh = FakeGh()
        gh.on("api graphql", {"data": {"repository": {"ref": None, "branch": None, "release": None, "pullRequests": {"nodes": []}}}})
        gh.on("actions/runs?branch=release/v0.9.40", {"total_count": 0, "workflow_runs": []})
        return gh

    receipt = json.loads((REAL / "receipt-0.9.40-pretag.json").read_text())
    for form in ("text", "json"):
        home = tmp_path / form
        home.mkdir()
        gh = scripted()
        rt = make(home, monkeypatch, gh)
        pypi(monkeypatch, {})
        shutil.copy(REAL / "receipt-0.9.40-pretag.json", statedir.root() / "release.json")
        (statedir.root() / "release-holds").mkdir(mode=0o700)
        (statedir.root() / "release-holds" / receipt["freeze_token"]).write_text("0.9.40")
        (statedir.root() / "merge-freeze").touch()  # an incident hold: never touched
        out = invoke(rt, ["--format", form, "release", "drop", "0.9.40", "--reason", "candidate red twice; cut again after the fix"])
        assert out.exit_code == 0, out
        archived = list((statedir.root() / "release-dropped").glob("v0.9.40-*.json"))
        assert len(archived) == 1 and not (statedir.root() / "release.json").exists() and list((statedir.root() / "release-holds").iterdir()) == []
        assert (statedir.root() / "merge-freeze").exists() and gh.writes() == []
        kept = json.loads(archived[0].read_text())
        assert kept["version"] == "0.9.40" and kept["freeze_token"] == receipt["freeze_token"] and kept["drop"]["reason"].startswith("candidate red twice")
        if form == "json":
            doc = area.ReleaseDrop.model_validate(json.loads(out.stdout))
            assert doc.dropped and doc.hold == "owned" and doc.archive == str(archived[0]) and doc.other_holds_remain and not doc.branch_deleted and doc.closed_pull_requests == []
        else:
            lines = out.stdout.splitlines()
            assert lines[0].startswith("DROPPED 0.9.40: its owned hold was removed; receipt archived at ") and lines[0].endswith("A fresh `sm release resume 0.9.40` starts clean.")
            assert lines[1].startswith("note: other merge holds remain")


def test_a_drop_archives_the_cut_state_so_a_later_cut_starts_fresh(tmp_path, monkeypatch):
    gh = FakeGh()
    gh.on("api graphql", {"data": {"repository": {"ref": None, "branch": None, "release": None, "pullRequests": {"nodes": []}}}})
    gh.on("actions/runs?branch=release/v0.9.40", {"total_count": 0, "workflow_runs": []})
    rt = make(tmp_path, monkeypatch, gh)
    pypi(monkeypatch, {})
    receipt = json.loads((REAL / "receipt-0.9.40-pretag.json").read_text())
    shutil.copy(REAL / "receipt-0.9.40-pretag.json", statedir.root() / "release.json")
    (statedir.root() / "release-holds").mkdir(mode=0o700)
    (statedir.root() / "release-holds" / receipt["freeze_token"]).write_text("0.9.40")
    cut = {"key": "0.9.40", "steps": {"admit prepare": {"status": "done", "detail": "prepared 71d394adb248"}}, "inputs": {"packet": "/p/packet.json"}}
    rt.providers.store.save(area.CUT_KIND, "0.9.40", cut)
    rt.providers.store.save(area.CUT_KIND, "0.9.41", {"key": "0.9.41"})  # another version's cut: untouched
    out = invoke(rt, ["--format", "json", "release", "drop", "0.9.40", "--reason", "candidate red twice; cut again after the fix"])
    assert out.exit_code == 0, out
    doc = area.ReleaseDrop.model_validate(json.loads(out.stdout))
    assert rt.providers.store.load(area.CUT_KIND, "0.9.40") is None and rt.providers.store.load(area.CUT_KIND, "0.9.41") == {"key": "0.9.41"}
    key = doc.cut_state_archived.removeprefix(area.CUT_ARCHIVE_KIND + "/")
    assert doc.cut_state_archived.startswith(area.CUT_ARCHIVE_KIND + "/0.9.40-") and rt.providers.store.load(area.CUT_ARCHIVE_KIND, key) == cut
    assert any("a later cut starts fresh" in line for line in doc.lines)


def test_a_refused_drop_leaves_the_cut_state_alone(tmp_path, monkeypatch):
    rt = make(tmp_path, monkeypatch, FakeGh())
    rt.providers.store.save(area.CUT_KIND, "0.9.40", {"key": "0.9.40"})
    out = invoke(rt, ["release", "drop", "0.9.40", "--reason", "candidate red twice; cut again after the fix"])
    assert out.exit_code != 0 and rt.providers.store.load(area.CUT_KIND, "0.9.40") == {"key": "0.9.40"}


def test_a_resume_is_refused_while_a_drop_is_recorded_but_unfinished(tmp_path, monkeypatch):
    rt = make(tmp_path, monkeypatch, FakeGh())
    (statedir.root() / "release.json").write_text(json.dumps({"version": "0.9.41", "freeze_token": "b" * 32, "drop": {"version": "0.9.41"}}))
    out = invoke(rt, ["release", "resume", "0.9.41"])
    assert out.exit_code == 1 and "a drop of 0.9.41 is recorded but unfinished; finish it first with `sm release drop 0.9.41 --reason ...`" in out.stdout


def test_the_steps_exposed_for_a_pipeline_are_the_runners_steps_in_its_order(tmp_path, monkeypatch):
    import inspect

    from steuermaschine.native import NativeRuntime, using

    rt = make(tmp_path, monkeypatch, FakeGh())
    with using(NativeRuntime(rt.providers, rt.settings, rt.project)):
        module = area.runner()
    assert module.hzlib.sleep == rt.providers.clock.sleep  # a pipeline's waits sleep on the clock provider too
    for _name, done, act in area.CUT_STEPS:
        assert all(callable(getattr(module, function)) for function in (*done, *act)), _name
    core = inspect.getsource(module.release_core)
    places = [core.index(marker) for marker in ('step("freeze"', 'step("main gate (train path)"', 'step("main tip green"', 'step("release PR open"',
                                                'step("previous release accepted"', 'step("release PR merged"', 'step("tag"', "lift_freeze_at_tag(", 'step("release published"',
                                                "pypi_step(", "compose_step(", "acceptance_step(", "verdict(run_id)")]
    assert places == sorted(places) and len(places) == len(area.CUT_STEPS)
    for function in ("acquire_global_lock", "route_to_tail", "free_global_lock", "free_tail_lock", "tail_receipt", "state", "remember"):
        assert callable(getattr(module, function))


def test_live_runs_the_mini_update_only_after_accepted_and_exits_on_its_proof(tmp_path, monkeypatch):
    from steuermaschine_heinzel.native import mini

    rt = make(tmp_path, monkeypatch, FakeGh())
    asked = []

    def proof(w, minimum=None):
        asked.append(minimum)
        return mini.MiniVerify(live=minimum == "0.9.41", version=minimum or "", reason="installed v0.9.40 does not satisfy minimum", updated=["kickstarted web"])
    monkeypatch.setattr(mini, "update", proof)
    from steuermaschine_heinzel.native import release_ios

    tested = []
    monkeypatch.setattr(release_ios, "after_accepted", lambda w, version: tested.append(version) or [f"IOS NOT-CLEAN {version}: testflight upload exited 70"])
    receipt: dict = {}
    monkeypatch.setattr(area, "receipt_of", lambda module, version: ("tail", receipt))
    monkeypatch.setattr(area, "run", lambda module, argv: (0, ["ACCEPTED 0.9.41 (run 7)"], []))
    receipt.update(version="0.9.41", finished=True, accepted=True, acceptance_run=7)
    out = invoke(rt, ["release", "resume", "0.9.41", "--live"])
    # A TestFlight failure is reported beside LIVE-OK and leaves the exit code alone (its alarm is the signal).
    assert out.exit_code == 0 and out.stdout.splitlines() == ["ACCEPTED 0.9.41 (run 7)", "update: kickstarted web", "plugins: ", "LIVE-OK 0.9.41",
                                                              "IOS NOT-CLEAN 0.9.41: testflight upload exited 70"] and asked == ["0.9.41"] == tested
    monkeypatch.setattr(mini, "update", lambda w, minimum=None: mini.MiniVerify(reason="the version served on port 8737 is 0.9.40, the installed one 0.9.41"))
    out = invoke(rt, ["release", "resume", "0.9.41", "--live"])
    assert out.exit_code == 1 and out.stdout.splitlines()[-2] == "NOT-LIVE: the version served on port 8737 is 0.9.40, the installed one 0.9.41"
    receipt.update(accepted=False)  # NOT ACCEPTED, unfinished or stopped: the Mini is never touched
    monkeypatch.setattr(area, "run", lambda module, argv: (1, ["NOT ACCEPTED 0.9.41 (run 7)"], []))
    monkeypatch.setattr(mini, "update", lambda *a, **k: pytest.fail("the Mini was touched"))
    monkeypatch.setattr(release_ios, "after_accepted", lambda *a, **k: pytest.fail("TestFlight was touched"))
    assert invoke(rt, ["release", "resume", "0.9.41", "--live"]).exit_code == 1
    assert invoke(rt, ["release", "resume", "patch", "--live"]).exit_code == 2
    assert invoke(rt, ["release", "resume", "0.9.41", "--pause", "--live"]).exit_code == 2
