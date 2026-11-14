"""mini verify, native. Compared with the old tool on the live Mini on 2026-11-12 23:00 (same five lines, LIVE-OK
0.9.39, with the remote URL set); the live status documents are private and are NOT recorded: the fakes below carry
only the fields the proof reads. No test starts a process or reads the network."""

import json
from pathlib import Path

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import mini
from steuermaschine_heinzel.plugin import plugin

NOW = 1_800_000_000
TICK = "2027-01-15T07:58:00+00:00"  # two minutes before NOW
REMOTE = "https://mini.example.ts.net/"


class Mini:
    """The scripted Mini: change an attribute to break one piece of evidence."""

    def __init__(self, tmp_path, remote=REMOTE):
        self.state = tmp_path / "state"
        (self.state / "release").mkdir(parents=True)
        self.receipt("0.9.39", 43158281029)
        self.cli = "0.9.39"
        self.web = {"installed": True, "loaded": True, "ready": True, "port": 8737, "instance": "a" * 32, "version": "0.9.39", "secret": "never shown"}
        self.daemon = {"installed": True, "owned": True, "loaded": True, "paused": False, "version_mismatch": False, "version": "0.9.39",
                       "installed_version": "0.9.39", "last_tick": TICK, "interval_minutes": 5}
        self.doctor = {"status": "up", "checks": {"system.daemon": [{"name": "last tick", "status": "up"}], "web": [{"name": "http", "status": "degraded"}]}}
        self.codes = {"http://127.0.0.1:8737/": "200", REMOTE: "200"}
        self.plugins = [{"distribution": "heinzel-google", "version": "0.9.1", "source_kind": "pypi"}, {"distribution": "other", "version": "1"}]
        self.commands = FakeCommands()
        c = self.commands
        c.on(["hz", "--version"], lambda argv: Result(0, f"hz {self.cli}\n"))
        c.on(["hz", "ui", "status"], lambda argv: Result(0, json.dumps(self.web)))
        c.on(["hz", "system", "daemon"], lambda argv: Result(0, json.dumps(self.daemon)))
        c.on(["hz", "system", "doctor"], lambda argv: Result(0, json.dumps(self.doctor)))
        c.on(["hz", "system", "plugin"], lambda argv: Result(0, json.dumps(self.plugins)))
        c.on(["curl"], lambda argv: Result(0, self.codes.get(argv[-1], "000")))
        stack = stack_of(plugin)
        table = {"legacy_state_dir": str(self.state), **({"mini_remote_url": remote} if remote else {})}
        settings, _ = stack.validate({"heinzel": table}, Path("."))
        providers = fake_providers(commands=c)
        providers.clock.t = NOW
        self.rt = Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, settings, _providers=providers)

    def receipt(self, version, run, **over):
        data = {"version": version, "accepted": True, "finished": True, "acceptance_run": run, "acceptance_run_id": run, **over}
        (self.state / "release" / f"v{version}.json").write_text(json.dumps(data))

    def run(self, *args):
        return invoke(self.rt, ["mini", "verify", *args])


def test_live_ok_prints_the_five_lines_of_the_old_tool(tmp_path):
    m = Mini(tmp_path)
    out = m.run()
    assert out.exit_code == 0, out
    assert out.stdout.splitlines() == ["latest=0.9.39 cli=0.9.39", "web=0.9.39 web_ok=True daemon=0.9.39 daemon_ok=True acceptance_run=43158281029",
                                       "local=200 tail=200 doctor=healthy", "plugins: google=0.9.1/pypi", "LIVE-OK 0.9.39"]
    assert "never shown" not in out.stdout
    assert {argv[0] for argv in m.commands.calls} == {"hz", "curl"} and not [a for a in m.commands.calls if a[0] == "hz" and a[1] not in ("--version", "ui", "system")]


def test_json_is_one_document_of_the_model(tmp_path):
    m = Mini(tmp_path)
    out = invoke(m.rt, ["--format", "json", "mini", "verify", "--minimum", "0.9.38"])
    doc = mini.MiniVerify.model_validate_json(out.stdout)
    assert out.exit_code == 0 and doc.live and doc.version == "0.9.39" and doc.target == "0.9.38" and doc.target_kind == "minimum" and doc.acceptance_run == 43158281029


def not_live(m, *args):
    out = m.run(*args)
    assert out.exit_code == 1, out
    return out.stdout.splitlines()


def test_an_installed_version_below_the_target_or_not_the_latest_accepted_is_not_live(tmp_path):
    m = Mini(tmp_path)
    assert not_live(m, "--minimum", "0.9.40") == ["minimum=0.9.40 cli=0.9.39", "NOT-LIVE: installed v0.9.39 does not satisfy minimum v0.9.40"]
    m.receipt("0.9.40", 5)
    assert not_live(m)[-1] == "NOT-LIVE: installed v0.9.39 does not satisfy latest v0.9.40"
    m.receipt("0.9.40", 5, accepted=False)  # a NOT ACCEPTED release is not the latest accepted one
    assert m.run().exit_code == 0


def test_receipts_must_be_positive_finished_consistent_and_regular_files(tmp_path):
    m = Mini(tmp_path)
    m.receipt("0.9.39", 7, absorbed_by="0.9.40")
    assert not_live(m, "--minimum", "0.9.39")[-1] == "NOT-LIVE: v0.9.39 has no finished positive acceptance verdict"
    assert not_live(m)[-1] == "NOT-LIVE: no finished accepted Core release receipt"
    m.receipt("0.9.39", 7)
    (m.state / "release.json").write_text(json.dumps({"version": "0.9.39", "accepted": True, "finished": True, "acceptance_run": 8}))
    assert not_live(m)[-1] == "NOT-LIVE: conflicting release receipts for v0.9.39"
    (m.state / "release.json").write_text(json.dumps({"version": "soon"}))
    assert not_live(m)[-1] == "NOT-LIVE: global release receipt has no exact version"
    (m.state / "release.json").unlink()
    (m.state / "release.json").symlink_to(m.state / "release" / "v0.9.39.json")
    assert not_live(m)[-1] == "NOT-LIVE: release.json is not a regular release receipt"


STAMPS = {"plugin-heinzel-clock-0.9.8": "2026-11-13T14:20:46Z", "plugin-heinzel-theme-0.1.5_run_id": 7,
          "plugin-heinzel-theme-0.1.5_dispatch": {"repo": "r", "sha": "a" * 40}}


def test_plugin_stamps_in_the_global_receipt_are_not_core_evidence(tmp_path):
    """heinzel-management-98cw: an older `sm release plugin` left only its stamps in the global receipt."""
    m = Mini(tmp_path)
    (m.state / "release.json").write_text(json.dumps(STAMPS))
    assert m.run().stdout.splitlines()[-1] == "LIVE-OK 0.9.39"
    assert m.run("--minimum", "0.9.39").exit_code == 0
    (m.state / "release" / "v0.9.39.json").unlink()  # the stamps never stand in for the tail receipt
    assert not_live(m)[-1] == "NOT-LIVE: no finished accepted Core release receipt"


def test_a_core_key_without_a_version_next_to_plugin_stamps_stays_not_live(tmp_path):
    m = Mini(tmp_path)
    (m.state / "release.json").write_text(json.dumps({**STAMPS, "freeze_token": "x"}))
    assert not_live(m)[-1] == "NOT-LIVE: global release receipt has no exact version"
    (m.state / "release.json").write_text(json.dumps({**STAMPS, "version": "0.9.39", "accepted": True, "finished": True, "acceptance_run": 8}))
    assert not_live(m)[-1] == "NOT-LIVE: conflicting release receipts for v0.9.39"


def test_web_and_daemon_must_be_owned_ready_on_the_version_with_a_fresh_tick(tmp_path):
    m = Mini(tmp_path)
    m.daemon["last_tick"] = "2027-01-15T07:00:00+00:00"  # an hour old at a 5 minute interval
    lines = not_live(m)
    assert lines[1] == "web=0.9.39 web_ok=True daemon=0.9.39 daemon_ok=False acceptance_run=43158281029"
    assert lines[-1] == "NOT-LIVE: web or daemon ownership, readiness, or running version is unverified" and len(lines) == 3
    m.daemon["last_tick"] = TICK
    m.web["version"] = "0.9.38-dirty"
    assert not_live(m)[1].startswith("web=? web_ok=False")
    m.web.update(version="0.9.39", port=9000)
    assert "web_ok=False" in not_live(m)[1]


def test_reachability_and_doctor(tmp_path):
    m = Mini(tmp_path)
    m.codes[REMOTE] = "502"
    assert not_live(m)[-2:] == ["local=200 tail=unverified doctor=healthy", "NOT-LIVE: HTTP reachability or doctor health is unverified"]
    m.codes[REMOTE] = "200"
    m.doctor["checks"]["system.daemon"][0]["status"] = "down"
    assert not_live(m)[-2] == "local=200 tail=200 doctor=unverified"
    unset = Mini(tmp_path / "b", remote=None)
    assert not_live(unset)[-2:] == ["local=200 tail=unverified doctor=healthy",
                                    "NOT-LIVE: the remote URL is not configured (setting mini_remote_url), so reachability for users is unverified"]


def test_a_missing_or_failing_command_is_named_and_a_bad_minimum_is_usage(tmp_path):
    m = Mini(tmp_path)
    m.commands.on(["hz", "ui", "status"], Result(1, "", "boom"))
    assert not_live(m)[-1] == "NOT-LIVE: hz ui status --json exited 1"
    m.commands.on(["hz", "--version"], Result(127, "", "not found"))
    assert not_live(m) == ["NOT-LIVE: hz --version unavailable"]
    out = m.run("--minimum", "0.9")
    assert out.exit_code == 2 and "--minimum requires an exact X.Y.Z" in out.stderr + out.stdout


def test_go_live_carries_the_proof_into_the_decision(tmp_path):
    from test_native_go import MAIN, run_row

    m = Mini(tmp_path)
    (m.state / "focused-admission-receipts").mkdir()
    c = m.commands
    c.on(["git", "-C"], Result(0, f"{MAIN}\trefs/heads/main\n"))
    c.on(["git", "-C", str(Path("~/Code/heinzel").expanduser()), "rev-list"], Result(0, "0\n"))
    c.on(["gh", "run", "list"], Result(0, json.dumps([run_row(1)])))
    c.on(["gh", "pr", "list"], Result(0, "[]"))
    c.on(["gh", "release", "list"], Result(0, json.dumps([{"tagName": "v0.9.39", "isLatest": True}])))
    c.on(["gh", "api"], Result(0, json.dumps({"variables": []})))
    c.on(["ps"], Result(0, ""))
    ok = invoke(m.rt, ["go", "--live"])
    assert "latest release v0.9.39; Mini CLI 0.9.39; live: LIVE-OK 0.9.39" in ok.stdout, ok
    m.codes[REMOTE] = "000"
    bad = invoke(m.rt, ["go", "--live"]).stdout.splitlines()
    assert bad[0] == "PHASE    v0.9.39 published, live proof missing: NOT-LIVE: HTTP reachability or doctor health is unverified" and bad[1] == "NEXT     sm mini verify --minimum 0.9.39"
