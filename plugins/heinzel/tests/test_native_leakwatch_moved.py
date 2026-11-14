"""The leak watch library, moved (leakwatch_lib/watch.py). These are the old tool's test cases word for word; only
the way the module, the thresholds and the state directory are found changed (three lines)."""

import json

import pytest

from steuermaschine_heinzel.native.leakwatch_lib import watch


@pytest.fixture
def tool():
    return lambda _name: watch


@pytest.fixture
def home(tmp_path):
    return tmp_path


PS = """\
 100     1 Ss   02-03:19:24   0:39.74  42960 489004912 /Users/x/.local/share/uv/tools/heinzel/bin/python -I -u -m heinzel ui --no-browser --port 8737
 101   100 S        05:00     0:01.00  20000 400000000 /usr/bin/node /x/mcp-server
 102   101 Z        04:00     0:00.00      0         0 (defunct)
 200     1 S        09:00     0:02.00  15000 300000000 /Users/x/.local/share/uv/tools/heinzel/bin/python -m heinzel worker
 300     1 S        09:00     0:02.00  15000 300000000 /usr/bin/other
"""
LSOF = """\
p100
fcwd
tDIR
n/Users/x
ftxt
tREG
n/bin/python
f0
tCHR
n/dev/null
f3
tPIPE
n
f4
tKQUEUE
n
f6
tIPv4
TST=LISTEN
n127.0.0.1:8737
f7
tIPv4
TST=CLOSE_WAIT
n127.0.0.1:8737->127.0.0.1:5000
f8
tIPv6
TST=ESTABLISHED
n[::1]:8737->[::1]:5001
p101
f0
tREG
n/tmp/x
f5
tIPv4
TST=LISTEN
n*:9999
"""
NETSTAT = """\
tcp4       0      0  127.0.0.1.8737         127.0.0.1.60001        TIME_WAIT
tcp4       0      0  127.0.0.1.8737         127.0.0.1.60002        TIME_WAIT
tcp4       0      0  127.0.0.1.4444         127.0.0.1.60003        TIME_WAIT
"""
THREADS = """\
USER   PID   TT   %CPU STAT PRI     STIME     UTIME COMMAND
x      100   ??    0.1 S    31T   0:39.74   3:32.90 python
       100         0.0 S    31T   0:00.07   0:00.05
       100         0.0 S    31T   0:00.00   0:00.00
x      101   ??    0.1 S    31T   0:00.74   0:01.90 node
"""


def cfg(tool):
    return dict(tool("hz-leakwatch").DEFAULTS, daemon_label="io.example.daemon")


def sample_from_fixtures(lw):
    procs = lw.parse_ps(PS)
    tree = lw.find_tree(procs, 100, None)
    return lw.build_sample(tree, list(tree), lw.parse_lsof(LSOF), lw.parse_threads(THREADS), {100: 1.5, 101: 0.5}, NETSTAT,
                           lw.find_orphans(procs, tree), [8737], "0.9.8", 1000.0)


def test_parsers_and_sample_from_fake_output(tool):
    lw = tool("hz-leakwatch")
    assert lw.parse_clock("02-03:19:24") == 2 * 86400 + 3 * 3600 + 19 * 60 + 24 and lw.parse_clock("0:39.74") == 39.74
    assert lw.parse_threads(THREADS) == {100: 3, 101: 1}
    s = sample_from_fixtures(lw)
    t = s["totals"]
    assert [p["role"] for p in s["procs"]] == ["server", "child", "child"]
    assert t["children"] == 2 and t["zombies"] == 1 and t["orphans"] == 1 and s["orphans"] == [200]  # 300 is not hz
    assert t["fds"] == 8 and t["fd_types"] == {"files": 2, "sockets": 4, "pipes": 1, "kqueue": 1, "other": 0}  # cwd/txt not counted
    assert s["tcp"] == {"LISTEN": 2, "CLOSE_WAIT": 1, "ESTABLISHED": 1, "TIME_WAIT": 2} and t["close_wait"] == 1
    assert s["listen"] == [8737, 9999] and s["uptime_s"] == 2 * 86400 + 3 * 3600 + 19 * 60 + 24 and s["version"] == "0.9.8"
    assert t["rss_kb"] == 62960 and t["threads"] == 4 and t["cpu"] == 2.0


def test_check_findings_from_fixture(tool):
    lw = tool("hz-leakwatch")
    found = "\n".join(lw.findings([sample_from_fixtures(lw)], cfg(tool), now=1000.0))
    assert "orphan" in found and "zombie" in found and "[9999]" in found
    assert "CLOSE_WAIT" not in found  # one is under the limit of 5


def series_samples(lw, values, step=300, uptime0=7200, **over):
    """Samples every 5 minutes with the given fd counts (rss follows as a function of fds)."""
    out = []
    for i, fds in enumerate(values):
        s = sample_from_fixtures(lw)
        s["ts"] = 1000.0 + i * step
        s["uptime_s"] = uptime0 + i * step
        s["orphans"], s["procs"] = [], s["procs"][:1]
        s["totals"].update(fds=fds, orphans=0, zombies=0, close_wait=0, children=0, cpu=0.5, rss_kb=50000)
        s["totals"].update(over)
        s["listen"] = [8737]
        out.append(s)
    return out


def test_cpu_alert_attributes_server_and_daemon_without_changing_threshold(tool):
    lw = tool("hz-leakwatch")
    samples = series_samples(lw, [16, 16, 16])
    for sample, cpu in zip(samples, (1.8, 1.9, 1.9), strict=True):
        sample["totals"]["cpu"] = cpu
        sample["procs"] = [{"role": "server", "cpu": cpu}]
    samples[-1]["totals"]["cpu"] += 91.9
    samples[-1]["procs"].append({"role": "daemon", "cpu": 91.9, "age_s": 1})
    lines = lw.findings(samples, cfg(tool), now=1600)
    alert = next(line for line in lines if line.startswith("CPU "))
    assert "CPU 32.5% sample mean from 3 intermittent samples in a 10 min lookback (2s target per sample)" in alert
    assert "limit 25% (mean contributions: server 1.9%, daemon 30.6%; daemon observed in 1/3 samples)" in alert
    assert "average over" not in alert
    samples[-1]["totals"]["children"] = 1
    assert not any(line.startswith("CPU ") for line in lw.findings(samples, cfg(tool), now=1600))


def test_cpu_alert_displays_small_threshold_exceedance(tool):
    lw = tool("hz-leakwatch")
    samples = series_samples(lw, [16, 16, 16], cpu=25.0)
    samples[-1]["totals"]["cpu"] = 25.1
    assert any(line.startswith("CPU >25% ") for line in lw.findings(samples, cfg(tool), now=1600))


def test_synthetic_leak_is_flagged_and_flat_series_is_not(tool):
    lw = tool("hz-leakwatch")
    c = cfg(tool)
    leak = series_samples(lw, [100 + 10 * i for i in range(8)])  # +10 per 5 min = +120/h
    now = leak[-1]["ts"]
    assert any("open fds grows monotonically" in f for f in lw.findings(leak, c, now))
    flat = series_samples(lw, [100] * 12)
    assert lw.findings(flat, c, flat[-1]["ts"]) == []
    noisy = series_samples(lw, [100, 104, 99, 105, 98, 103, 99, 104])
    assert lw.findings(noisy, c, noisy[-1]["ts"]) == []
    short = series_samples(lw, [100, 150, 200, 250, 300])  # only 5 samples: not enough evidence
    assert lw.findings(short, c, short[-1]["ts"]) == []


def test_rss_growth_and_absolute_limits(tool):
    lw = tool("hz-leakwatch")
    c = cfg(tool)
    grow = series_samples(lw, [100] * 8)
    for i, s in enumerate(grow):
        s["totals"]["rss_kb"] = 100_000 + i * 10_000  # 10 MB per 5 min
    assert any("RSS MB grows" in f for f in lw.findings(grow, c, grow[-1]["ts"]))
    big = series_samples(lw, [600], rss_kb=900 * 1024)
    out = lw.findings(big, c, big[-1]["ts"])
    assert any("600 open fds" in f for f in out) and any("RSS 900 MB" in f for f in out)


def test_idle_cpu_not_running_and_restart(tool):
    lw = tool("hz-leakwatch")
    c = cfg(tool)
    busy = series_samples(lw, [100] * 4, cpu=60.0)
    assert any("CPU 60% sample mean from 3 intermittent samples" in f for f in lw.findings(busy, c, busy[-1]["ts"]))
    working = series_samples(lw, [100] * 4, cpu=60.0, children=2)
    assert not any("CPU" in f for f in lw.findings(working, c, working[-1]["ts"]))
    assert lw.findings([{"ts": 1.0, "running": False}], c, 2.0)[0].startswith("hz not running")
    two = series_samples(lw, [100, 100])
    two[1]["uptime_s"], two[1]["version"] = 60, "0.9.9"
    assert any("restarted" in f for f in lw.findings(two, c, two[-1]["ts"]))

    two[1]["uptime_s"] = two[0]["uptime_s"] + 300
    assert lw.findings(two, c, two[-1]["ts"]) == [
        "hz package on disk changed: 0.9.8->0.9.9; server restart not observed"
    ]


def test_rss_recovery_keeps_warning_and_shows_decision_slope_and_prior_peak(tool):
    lw = tool("hz-leakwatch")
    # A residency drop followed by recovery and a plateau can pass the unchanged
    # detector even though the process is below its earlier RSS high-water mark.
    values = [66, 34, 34, 52, 52, 64, 64, 64, 64]
    samples = series_samples(lw, [100] * len(values))
    for sample, rss in zip(samples, values, strict=True):
        sample["totals"]["rss_kb"] = rss * 1024
    tr = lw.trend(lw.series(samples, "rss_mb"), samples, cfg(tool), "rss_mb")
    assert tr["leak"] and tr["run"] == 8
    assert tr["run_slope"] > cfg(tool)["rss_slope_mb_per_hour"]
    warning, = lw.findings(samples, cfg(tool), samples[-1]["ts"])
    assert f"sample-window tail {tr['run_slope']:+.1f}/h" in warning
    assert "may span restarts" in warning
    assert "current tree RSS 64.0 MB is at/below earlier 66.0 MB tree peak during this server uptime" in warning
    assert "residency or tree membership changes can also trigger this warning" in warning


def test_rss_context_does_not_compare_previous_process_or_hide_new_high(tool):
    lw = tool("hz-leakwatch")
    samples = series_samples(lw, [100] * 9)
    for i, sample in enumerate(samples):
        sample["totals"]["rss_kb"] = (200 if i == 0 else 100 + i) * 1024
    assert "tree peak during this server uptime" in lw.rss_context(samples, 8)
    samples[0]["server_pid"] += 1
    assert lw.rss_context(samples, 8) == ""
    samples[0]["server_pid"] -= 1
    samples[-1]["totals"]["rss_kb"] = 201 * 1024
    assert lw.rss_context(samples, 8) == ""


def test_rss_context_describes_changed_children_as_tree_totals(tool):
    lw = tool("hz-leakwatch")
    samples = series_samples(lw, [100] * 9)
    for sample in samples:
        sample["totals"]["rss_kb"] = 64 * 1024
        sample["procs"][0]["rss_kb"] = 64 * 1024
    # The earlier peak belongs to a child that is gone, not to the server.
    samples[0]["procs"].append({"pid": 456, "role": "child", "rss_kb": 100 * 1024})
    samples[0]["totals"].update(rss_kb=164 * 1024, procs=2, children=1)
    context = lw.rss_context(samples, 8)
    assert "current tree RSS 64.0 MB" in context
    assert "earlier 164.0 MB tree peak during this server uptime" in context
    assert "tree membership changes" in context
    assert "same-process" not in context


def test_sample_writes_jsonl_and_rotates(tool, home, monkeypatch):
    lw = tool("hz-leakwatch")
    monkeypatch.setattr(lw, "EDGE", {"state": str(home / "state")})
    monkeypatch.setattr(lw, "MAX_BYTES", 200)
    calls = []

    def fake_run(cmd):
        calls.append(cmd[0])
        return {"lsof": "", "ps": PS if "-axo" in cmd else "", "launchctl": "", "netstat": NETSTAT}[cmd[0]]

    monkeypatch.setattr(lw, "run", fake_run)
    monkeypatch.setattr(lw, "parse_cpu", lambda _t: {100: 40.0})
    sample = lw.take_sample(cfg(tool), sleep=lambda _s: None)
    assert sample["running"] is False  # no listener in the fake lsof, no daemon: nothing to follow
    lw.append_sample(sample)
    lw.append_sample(sample)
    assert (home / "state/samples.jsonl.1").exists() and len(lw.read_samples()) == 2
    assert all(c in ("lsof", "ps", "launchctl") for c in calls)  # read-only commands only
    assert json.loads((home / "state/samples.jsonl").read_text().splitlines()[0])["running"] is False


def test_report_table_lists_metrics(tool):
    lw = tool("hz-leakwatch")
    leak = series_samples(lw, [100 + 10 * i for i in range(8)])
    text = "\n".join(lw.table(leak, cfg(tool)))
    assert "open fds" in text and "LEAK?" in text and "RSS MB" in text and "ok" in text


def test_check_flags_high_load_and_a_web_that_does_not_answer(tool):
    lw = tool("hz-leakwatch")
    samples = series_samples(lw, [100] * 3)
    samples[-1].update(load1=82.0, web_s=None)
    found = lw.findings(samples, cfg(tool), now=samples[-1]["ts"])
    assert any("load 82 above 40" in f for f in found)
    assert any("did not answer within 5 s" in f for f in found)
    samples[-1].update(load1=12.0, web_s=0.2)
    found = lw.findings(samples, cfg(tool), now=samples[-1]["ts"])
    assert not any("load" in f or "did not answer" in f for f in found)


def test_sampling_records_load_and_probes_the_web_only_when_hz_runs(tool, monkeypatch):
    lw = tool("hz-leakwatch")
    probed = []
    monkeypatch.setattr(lw, "_take_sample", lambda *a: {"running": True})
    sample = lw.take_sample(cfg(tool), probe=lambda port: probed.append(port) or 0.3, loadavg=lambda: (33.33, 0, 0))
    assert sample["load1"] == 33.3 and sample["web_s"] == 0.3 and probed == [cfg(tool)["port"]]
    monkeypatch.setattr(lw, "_take_sample", lambda *a: {"running": False})
    assert lw.take_sample(cfg(tool), probe=lambda port: probed.append(port), loadavg=lambda: (1, 0, 0))["web_s"] is None and len(probed) == 1
