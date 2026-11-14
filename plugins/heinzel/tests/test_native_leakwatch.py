"""mini leakwatch report | check | sample, native. Compared live on 2026-11-12 23:55 with the old tool's `report` for
the running product (72 samples): the same seven lines. No test here starts a process or reads the network."""

import json
from pathlib import Path

import test_native_leakwatch_moved as moved

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import leakwatch
from steuermaschine_heinzel.native.leakwatch_lib import watch
from steuermaschine_heinzel.plugin import plugin


def runtime_for(tmp_path, commands=None, reviewed=True, **settings):
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": {"leakwatch_state_dir": str(tmp_path / "leak"), **settings}}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed)
    return Runtime(project, stack, validated, _providers=fake_providers(commands=commands or FakeCommands()))


def quiet_machine() -> FakeCommands:
    """A machine where the product does not run: no listener on the port, no daemon job."""
    c = FakeCommands()
    c.on(["lsof"], Result(1, "", ""))
    c.on(["launchctl", "list"], Result(113, "", "Could not find service"))
    c.on(["ps"], Result(0, moved.PS, ""))
    return c


def stored(tmp_path, rt, values, **over):
    """Write running samples that end now, five minutes apart."""
    samples = moved.series_samples(watch, values, **over)
    shift = rt.providers.clock.now() - samples[-1]["ts"]
    (tmp_path / "leak").mkdir(exist_ok=True)
    (tmp_path / "leak" / "samples.jsonl").write_text("".join(json.dumps({**s, "ts": s["ts"] + shift}) + "\n" for s in samples))


def test_report_is_the_table_and_json_carries_the_metrics(tmp_path):
    rt = runtime_for(tmp_path)
    stored(tmp_path, rt, [100 + 10 * i for i in range(8)])
    out = invoke(rt, ["mini", "leakwatch", "report"])
    lines = out.stdout.splitlines()
    assert out.exit_code == 0 and lines[0].startswith("hz (on disk 0.9.8) pid 100 up ") and lines[1].split() == ["metric", "now", "slope/h", "samples", "verdict"]
    assert lines[2].startswith("open fds") and "LEAK? monotonic over 8 samples" in lines[2] and lines[-1].startswith("RSS MB") and lines[-1].endswith("ok")
    report = leakwatch.LeakReport.model_validate_json(invoke(rt, ["--format", "json", "mini", "leakwatch", "report", "--hours", "1"]).stdout)
    assert report.running and report.samples == 8 and report.version == "0.9.8" and report.server_pid == 100
    assert [(m.metric, m.leak) for m in report.metrics][:2] == [("open fds", True), ("sockets", False)] and report.metrics[0].now == 170 and report.read_only


def test_report_without_samples(tmp_path):
    out = invoke(runtime_for(tmp_path), ["mini", "leakwatch", "report"])
    assert out.exit_code == 0 and out.stdout.strip() == "hz not running"
    report = leakwatch.LeakReport.model_validate_json(invoke(runtime_for(tmp_path), ["--format", "json", "mini", "leakwatch", "report"]).stdout)
    assert not report.running and report.metrics == []


def test_check_is_ok_on_fresh_samples_and_names_a_leak(tmp_path):
    commands = FakeCommands()
    rt = runtime_for(tmp_path, commands)
    stored(tmp_path, rt, [100] * 8)
    out = invoke(rt, ["mini", "leakwatch", "check"])
    assert out.exit_code == 0 and out.stdout.strip() == "ok" and commands.calls == []  # fresh samples: nothing is run
    stored(tmp_path, rt, [100 + 10 * i for i in range(8)])
    out = invoke(rt, ["mini", "leakwatch", "check"])
    assert out.exit_code == 1 and out.stdout.startswith("open fds grows monotonically: +70 over 8 samples")
    doc = leakwatch.LeakCheck.model_validate_json(invoke(rt, ["--format", "json", "mini", "leakwatch", "check"]).stdout)
    assert doc.judged_on == "samples" and doc.samples == 8 and doc.newest_sample_age_minutes == 0.0 and len(doc.findings) == 1


def test_a_stale_sampler_is_judged_on_one_live_sample_that_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "probe_web", lambda port: 1 / 0)  # the product does not run: the web is never asked
    commands = quiet_machine()
    rt = runtime_for(tmp_path, commands)
    out = invoke(rt, ["--format", "json", "mini", "leakwatch", "check"])
    doc = leakwatch.LeakCheck.model_validate_json(out.stdout)
    assert out.exit_code == 1 and doc.findings == ["hz not running (no server on port 8737)"] and doc.judged_on == "live" and doc.newest_sample_age_minutes is None
    assert not (tmp_path / "leak").exists()  # a read writes nothing
    assert {argv[0] for argv in commands.calls} == {"lsof", "launchctl", "ps"}
    assert ["launchctl", "list", "io.github.helmut-hoffer-von-ankershoffen.heinzel.daemon"] in commands.calls


def test_sample_appends_one_line_and_is_off_without_its_review(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "probe_web", lambda port: 1 / 0)
    rt = runtime_for(tmp_path, quiet_machine(), leakwatch_daemon_label="io.example.daemon")
    out = invoke(rt, ["mini", "leakwatch", "sample"])
    assert out.exit_code == 0 and out.stdout.strip().endswith("Z running=False procs=0 fds=0 rss=0MB load=" + out.stdout.split("load=")[1].strip())
    assert ["launchctl", "list", "io.example.daemon"] in rt.providers.commands.calls
    doc = leakwatch.LeakSample.model_validate_json(invoke(rt, ["--format", "json", "mini", "leakwatch", "sample"]).stdout)
    lines = (tmp_path / "leak" / "samples.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["running"] is False and doc.file == str(tmp_path / "leak" / "samples.jsonl") and doc.web_seconds is None
    # an unreviewed state, set explicitly: the test must not depend on what the review file says today
    from steuermaschine.native import NativeCommand
    monkeypatch.setattr(NativeCommand, "review_state", lambda self: ("open", "no verdict"))
    blocked = invoke(runtime_for(tmp_path, quiet_machine(), reviewed=False), ["mini", "leakwatch", "sample"])
    assert blocked.exit_code == 70 and len((tmp_path / "leak" / "samples.jsonl").read_text().splitlines()) == 2


def test_thresholds_can_be_overridden_and_an_unset_state_directory_refuses(tmp_path):
    override = tmp_path / "leak.toml"
    override.write_text("fds_max = 50\n")
    rt = runtime_for(tmp_path, leakwatch_config_file=str(override))
    stored(tmp_path, rt, [100] * 8)
    out = invoke(rt, ["mini", "leakwatch", "check"])
    assert out.exit_code == 1 and out.stdout.strip() == "100 open fds, limit 50"
    for args in (["mini", "leakwatch", "check"], ["mini", "leakwatch", "report"], ["mini", "leakwatch", "sample"]):
        out = invoke(runtime_for(tmp_path, leakwatch_state_dir=None), args)
        assert out.exit_code == 1 and "leakwatch_state_dir is not set" in out.stderr


def test_a_running_product_is_sampled_with_read_only_commands_and_one_local_get(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "probe_web", lambda port: 0.21)
    c = FakeCommands()
    c.on(["lsof", "-nP", "-iTCP:8737"], Result(0, "100\n", ""))
    c.on(["lsof", "-nP", "-p"], Result(0, moved.LSOF, ""))
    c.on(["launchctl", "list"], Result(0, '{\n\t"PID" = 101;\n};\n', ""))
    c.on(["ps", "-axo"], Result(0, moved.PS, ""))
    c.on(["ps", "-o"], Result(0, "  100   0:40.74\n  101   0:00.10\nnoise\n", ""))
    c.on(["ps", "-M"], Result(0, moved.THREADS, ""))
    c.on(["netstat"], Result(0, moved.NETSTAT, ""))
    rt = runtime_for(tmp_path, c)
    doc = leakwatch.LeakSample.model_validate_json(invoke(rt, ["--format", "json", "mini", "leakwatch", "sample"]).stdout)
    assert doc.running and doc.processes == 3 and doc.fds == 8 and doc.web_seconds == 0.21 and rt.providers.clock.slept == [watch.CPU_SECONDS]
    stored_sample = json.loads((tmp_path / "leak" / "samples.jsonl").read_text())
    assert stored_sample["server_pid"] == 100 and stored_sample["orphans"] == [200] and stored_sample["listen"] == [8737, 9999]
    assert {argv[0] for argv in c.calls} == {"lsof", "launchctl", "ps", "netstat"}  # nothing that signals, restarts or opens the product


def test_the_web_probe_and_the_version_on_disk(tmp_path):
    assert watch.probe_web(1) is None  # nothing listens on port 1 of the loopback interface
    site = tmp_path / "tool" / "lib" / "python3.14" / "site-packages" / "heinzel-0.9.39.dist-info"
    site.mkdir(parents=True)
    assert watch.hz_version(f"{tmp_path}/tool/bin/python -m heinzel serve") == "0.9.39"
    assert watch.hz_version("/usr/bin/true") is None and watch.hz_version(f"{tmp_path}/other/bin/python x") is None
    assert watch.parse_cpu("  7 0:01.50\nbad line here\n") == {7: 1.5} and watch.parse_ps("x\n") == {}
