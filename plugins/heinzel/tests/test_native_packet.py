"""Native packet commands through the command tree (text and json), on the scratch Core-shaped repository of the
moved manifest tests. No network: GitHub answers come from FakeCommands; git runs on temporary repositories only."""

import json
from pathlib import Path

import pytest
from test_native_packet_manifest import _git, _write, core, generate  # noqa: F401  (core is a fixture)

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel import native
from steuermaschine_heinzel.native.packet_lib import floor_check, ops, wave_size
from steuermaschine_heinzel.plugin import plugin
from steuermaschine_heinzel.toolmap import ROWS

HEAD, MAIN = "a" * 40, "b" * 40
PROTECTED = ["scripts/focused_plan.py", "uv.lock"]


def runtime_for(core_dir, commands=None, **settings):
    stack = stack_of(plugin)
    table, _ = stack.validate({"heinzel": {"core_repo_dir": str(core_dir), "repo": "o/r", **settings}}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, table, _providers=fake_providers(commands=commands or FakeCommands()))


def document(out, model):
    assert out.stdout.count("\n") == 1, out
    return model.model_validate(json.loads(out.stdout))


# ----------------------------------------------------------------------------- head manifest
def test_manifest_builds_and_verifies_in_text_and_json(core):  # noqa: F811
    rt, packet = runtime_for(core["repo"]), core["tmp"] / "packet.json"
    out = invoke(rt, ["head", "manifest", "feature", "--base", core["base"], "--junit", f"python={core['junit']}", "--output", str(packet)])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == f"head {core['head']}" and lines[-1].startswith(f"wrote {packet} sha256 ")
    old = core["tmp"] / "old.json"
    assert generate(core, out="old.json")[0] == 0 and old.read_bytes() == packet.read_bytes()  # the moved argv form writes the same bytes
    built = document(invoke(rt, ["--format", "json", "head", "manifest", "feature", "--base", core["base"], "--junit", f"python={core['junit']}", "-o", str(packet)]),
                     ops.ManifestResult)
    assert built.status == "built" and built.head == core["head"] and built.output == str(packet) and built.layout == "indent"
    assert built.packet_sha256 == lines[-1].split()[-1] and "selection: derived from the repository selector" in built.summary
    out = invoke(rt, ["head", "manifest", "--verify", str(packet)])
    assert out.exit_code == 0 and out.stdout.splitlines()[-1] == f"BYTE-EQUAL sha256 {built.packet_sha256}"
    verified = document(invoke(rt, ["--format", "json", "head", "manifest", "--verify", str(packet), "--junit", f"python={core['junit']}"]), ops.ManifestResult)
    assert verified.status == "byte_equal" and verified.exit_code == 0 and verified.review_path == ""


def test_manifest_binds_a_review_and_keeps_the_canonical_layout(core):  # noqa: F811
    rt, packet = runtime_for(core["repo"]), core["tmp"] / "packet.json"
    (core["tmp"] / "REVIEW.md").write_text("reviewed\n")
    args = ["head", "manifest", core["head"], "--base", core["base"], "--junit", f"python={core['junit']}", "--output", str(packet), "--layout", "canonical-nl"]
    out = invoke(rt, [*args, "--review", "REVIEW.md"])
    assert out.exit_code == 1 and "--review needs --review-root" in out.stderr and not packet.exists()
    built = document(invoke(rt, ["--format", "json", *args, "--review", "REVIEW.md", "--review-root", str(core["tmp"])]), ops.ManifestResult)
    assert built.review_path == "REVIEW.md" and built.layout == "canonical-nl" and packet.read_bytes().endswith(b"}\n") and b"\n " not in packet.read_bytes()
    assert document(invoke(rt, ["--format", "json", "head", "manifest", "--verify", str(packet)]), ops.ManifestResult).layout == "canonical-nl"
    (core["tmp"] / "REVIEW.md").write_text("changed after the build\n")
    out = invoke(rt, ["head", "manifest", "--verify", str(packet)])
    assert out.exit_code == 1 and out.stdout.splitlines()[-1] == "DIFFERENT review.sha256"
    data = json.loads(packet.read_bytes())
    packet.write_text(json.dumps(data))
    (core["tmp"] / "REVIEW.md").write_text("reviewed\n")
    different = document(invoke(rt, ["--format", "json", "head", "manifest", "--verify", str(packet)]), ops.ManifestResult)
    assert different.status == "different" and different.differences == ["DIFFERENT formatting only (same JSON value)"]


def test_manifest_refuses_on_stderr_or_with_one_error_document(core):  # noqa: F811
    rt, packet = runtime_for(core["repo"]), core["tmp"] / "packet.json"
    out = invoke(rt, ["head", "manifest", "feature", "--base", core["base"], "--output", str(packet)])
    assert out.exit_code == 1 and "missing ['python']" in out.stderr and out.stdout == "" and not packet.exists()
    out = invoke(rt, ["--format", "json", "head", "manifest", "feature", "--base", core["base"], "--output", str(packet), "--layout", "wide"])
    assert out.exit_code == 1 and "unknown layout wide" in json.loads(out.stdout)["error"]
    for args in (["head", "manifest"], ["head", "manifest", "feature", "--verify", str(packet)], ["head", "manifest", "feature"]):
        out = invoke(rt, args)
        assert out.exit_code == 2 and ("exactly one of TARGET or --verify" in out.stderr or "needs --output" in out.stderr)
    out = invoke(rt, ["head", "manifest", "--verify", str(core["tmp"] / "absent.json")])
    assert out.exit_code == 1 and "No such file" in out.stderr


def test_manifest_reads_the_head_of_a_pr_number_through_the_one_client(core):  # noqa: F811
    commands = FakeCommands()
    commands.on(["gh", "pr", "view", "12"], Result(0, json.dumps({"headRefOid": core["head"]}), ""))
    commands.on(["gh", "pr", "view", "13"], Result(1, "", "HTTP 404: Not Found"))
    rt, packet = runtime_for(core["repo"], commands), core["tmp"] / "packet.json"
    out = invoke(rt, ["head", "manifest", "12", "--base", core["base"], "--junit", f"python={core['junit']}", "--output", str(packet)])
    assert out.exit_code == 0 and json.loads(packet.read_bytes())["manifest"]["head"] == core["head"]
    assert ["gh", "pr", "view", "12", "--json", "headRefOid"] in commands.calls
    out = invoke(rt, ["head", "manifest", "13", "--base", core["base"], "--output", str(packet)])
    assert out.exit_code == 1 and "cannot read the head of PR 13: HTTP 404: Not Found" in out.stderr


# ----------------------------------------------------------------------------- head size
def test_size_of_a_real_branch_in_text_and_json_equals_the_moved_report(core):  # noqa: F811
    rt, printed = runtime_for(core["repo"]), []
    assert wave_size.run(["feature"], str(core["repo"]), core["base"], printed.append) == 0
    out = invoke(rt, ["head", "size", "feature", "--base", core["base"]])
    assert out.exit_code == 0 and out.stdout.splitlines() == printed and printed[-1] == "FITS"
    size = document(invoke(rt, ["--format", "json", "head", "size", f"{core['repo']}:feature", "--core", "/nowhere", "--base", core["base"]]), ops.SizeResult)
    assert size.fits and size.union == {"python": 1} and size.limits == {"python": None, "production": None} and size.production == 1
    assert size.branches[0].head == core["head"] and size.branches[0].files == 2 and size.branches[0].production == ["src/heinzel/mod.py"]
    assert size.base == core["base"] and size.lines == printed


def test_size_refuses_a_conflicting_wave_and_names_the_branch_to_move(core):  # noqa: F811
    repo = core["repo"]
    _git(repo, "checkout", "-qb", "other", core["base"])
    _write(repo, {"src/heinzel/mod.py": "x = 3\n"})
    _git(repo, "commit", "-qam", "other")
    rt, printed = runtime_for(repo), []
    assert wave_size.run(["feature", "other"], str(repo), core["base"], printed.append) == 1
    out = invoke(rt, ["head", "size", "feature", "other", "--base", core["base"]])
    assert out.exit_code == 1 and out.stdout.splitlines() == printed
    size = document(invoke(rt, ["--format", "json", "head", "size", "feature", "other", "--base", core["base"]]), ops.SizeResult)
    assert not size.fits and size.conflicts == [["feature", "other"]] and size.move == "other" and size.problems == ["conflict feature x other"]
    assert [(o.first, o.second, o.files) for o in size.overlaps] == [("feature", "other", ["src/heinzel/mod.py"])]
    assert size.lines[-2:] == ["REFUSED: conflict feature x other", "MOVE other to the next wave"]
    out = invoke(rt, ["head", "size", "feature", "--base", "no-such-rev"])
    assert out.exit_code == 1 and "refused: git rev-parse failed" in out.stderr


def test_size_has_no_lane_maximum(core):  # noqa: F811
    """Owner 2026-11-14 (PRs of any size): a web lane far over the base's old MAX_IDS fits."""
    facts = {"a": {"branch": "a", "head": HEAD, "conflict": "", "changed": {"x": "M"}, "selection": {"web": [f"web/a{i}.test.ts" for i in range(300)]},
                   "production": [], "protected": [], "reasons": []}}
    size = ops.size_wave(["a"], core["repo"], core["base"], lambda repo, base, branch, policy: facts[branch], lambda repo, facts: [])
    assert size.fits and size.problems == [] and size.exit_code == 0 and size.limits["web"] is None


# ----------------------------------------------------------------------------- head floor
def floor_commands(draft=False, checks=("python",)):
    commands = FakeCommands()
    commands.on(["git"], lambda argv: Result(0, (HEAD if argv[-1].startswith("feat") else MAIN if argv[-1].endswith("^{commit}") else "1" * 40) + "\n", ""))
    commands.on(["gh", "pr", "list"], Result(0, json.dumps([{"number": 5, "state": "OPEN", "isDraft": draft, "baseRefName": "main", "headRefOid": HEAD}]), ""))
    commands.on(["gh", "api", "repos/o/r/actions/variables?per_page=100"], Result(0, json.dumps({"variables": [{"name": "FOCUSED_CI_PLANS", "value": "{}"}]}), ""))
    commands.on(["gh", "api", f"repos/o/r/commits/{HEAD}/check-runs?per_page=100"],
                Result(0, json.dumps({"check_runs": [{"name": f"Focused tests ({lane})", "conclusion": "success"} for lane in checks]}), ""))
    return commands


@pytest.fixture
def plan(monkeypatch):
    decoded = {"head": HEAD, "base": MAIN, "production": {}, "selection": {"python": ["tests/x_test.py"]}, "execution": {p: "1" * 40 for p in PROTECTED}}
    monkeypatch.setattr(floor_check, "decode_plan", lambda repo, legacy, slots, key: (decoded, PROTECTED, None))
    return decoded


def test_floor_ready_in_text_and_json(plan, tmp_path):
    commands = floor_commands()
    rt = runtime_for(tmp_path / "core", commands)
    out = invoke(rt, ["head", "floor", "feat/x"])
    assert out.exit_code == 0 and out.stdout == "READY\n", out
    ready = document(invoke(rt, ["--format", "json", "head", "floor", "feat/x"]), ops.FloorResult)
    assert ready.ready and ready.pr == 5 and ready.head == HEAD and ready.main == MAIN and ready.reasons == []
    assert all(call[0] in {"git", "gh"} for call in commands.calls) and ["gh", "pr", "list", "-R", "o/r", "--head", "feat/x", "--state", "open", "--json",
                                                                         "number,state,isDraft,baseRefName,headRefOid"] in commands.calls


def test_floor_not_ready_says_every_reason_and_exits_1(plan, tmp_path):
    rt = runtime_for(tmp_path / "core", floor_commands(draft=True, checks=()))
    out = invoke(rt, ["head", "floor", "feat/x", "--repo", "o/r", "--core", str(tmp_path)])
    assert out.exit_code == 1 and out.stdout.startswith("NOT READY: PR #5 is a draft") and "`Focused tests (python)` on the head is absent" in out.stdout
    not_ready = document(invoke(rt, ["--format", "json", "head", "floor", "feat/x"]), ops.FloorResult)
    assert not not_ready.ready and len(not_ready.reasons) == 2 and not_ready.exit_code == 1


def test_floor_refuses_when_state_is_unreadable(plan, tmp_path):
    commands = floor_commands()
    commands.on(["gh", "pr", "list"], Result(1, "", "HTTP 401: Bad credentials"))
    out = invoke(runtime_for(tmp_path / "core", commands), ["head", "floor", "feat/x"])
    assert out.exit_code == 1 and "GitHub read failed (pr list): HTTP 401: Bad credentials" in out.stderr and out.stdout == ""
    commands = floor_commands()
    commands.on(["git"], Result(128, "", "fatal: not a git repository"))
    out = invoke(runtime_for(tmp_path / "core", commands), ["head", "floor", "feat/x"])
    assert out.exit_code == 1 and "git -C failed: fatal: not a git repository" in out.stderr


def test_floor_decodes_the_plan_with_the_base_selector_in_an_isolated_interpreter(core, tmp_path):  # noqa: F811
    repo = core["repo"]
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"scripts/focused_plan.py": "import json\nPROTECTED_FILES = ('uv.lock',)\nVITEST_ALL = 'web/ALL'\n"
                                             "def plan_registry(legacy, slots):\n    return json.loads(legacy) | {'slots': len(slots)}\n"})
    _git(repo, "commit", "-qam", "registry reader")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    key = f"pull_request:{HEAD}"
    assert floor_check.decode_plan(str(repo), json.dumps({key: {"head": HEAD}}), ("a", "b"), key) == ({"head": HEAD}, ["uv.lock"], "web/ALL")
    assert floor_check.decode_plan(str(repo), "{}", (), key)[0] is None
    with pytest.raises(floor_check.Unreadable, match="registry unreadable"):
        floor_check.decode_plan(str(repo), "not json", (), key)
    with pytest.raises(floor_check.Unreadable, match="cannot archive"):
        floor_check.decode_plan(str(tmp_path), "{}", (), key)


def test_floor_takes_a_worktree_path_and_reports_a_missing_pr_plan_and_pin(core):  # noqa: F811
    repo = core["repo"]
    _git(repo, "update-ref", "refs/remotes/origin/main", core["base"])
    decoded = {"head": core["head"], "base": "c" * 40, "production": {}, "selection": {}, "execution": {"uv.lock": "0" * 40, "absent.txt": "1" * 40}}
    result = ops.floor(str(repo), repo, "o/r", "origin/main", git=lambda cmd: floor_check.run(cmd), gh=lambda cmd: "[]" if cmd[1] == "pr" else json.dumps(
        {"variables": [], "check_runs": []}), decode=lambda *args: (decoded, ["uv.lock", "absent.txt"]))
    assert result.head == core["head"] and result.main == core["base"] and result.pr is None
    assert result.reasons == ["no open PR for the branch; open one and mark it ready"]
    facts = floor_check.gather(str(repo), str(repo), "o/r", "origin/main", floor_check.run,
                               lambda cmd: json.dumps([{"number": 7, "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefOid": core["head"]}])
                               if cmd[1] == "pr" else json.dumps({"variables": [], "check_runs": []}), lambda *args: (decoded, ["uv.lock", "absent.txt"]))
    assert facts["blobs"]["absent.txt"] == "" and not facts["base_is_ancestor"]
    assert floor_check.evaluate(facts) == [f"plan base {'c' * 10} is neither origin/main nor its ancestor; re-admit on current main",
                                           "protected file differs from the plan pin: absent.txt", "protected file differs from the plan pin: uv.lock"]


# ----------------------------------------------------------------------------- registration
def test_registrations_and_tool_map():
    reg = {c.path: c for c in native.commands() if hasattr(c, "writes")}
    assert reg["head manifest"].writes is True  # it writes the packet file
    for path in ("head size", "head floor"):
        assert reg[path].writes is False
    rows = {row.tool: row.path for row in ROWS}
    assert rows["hz-focused-manifest"] == "head manifest" and rows["hz-wave-size"] == "head size" and rows["hz-floor-check"] == "head floor"
