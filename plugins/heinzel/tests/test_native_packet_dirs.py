"""`sm head packet preopen|predict|bind|wave` through the command tree, on the scratch Core-shaped repository of the
moved builder tests. The collection is the real one (pytest --collect-only on the extracted tree) under the real run
lock, in a sandbox HOME with its own lock files. No network."""

import json
import re
import shlex
from pathlib import Path

import pytest
from test_native_packet_wave import _git, squash, wave  # noqa: F401  (wave is a fixture)

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.defaults import Subprocess, SystemClock
from steuermaschine.plugins import stack_of
from steuermaschine.runlock import RunLock
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel import native
from steuermaschine_heinzel.native import packet
from steuermaschine_heinzel.native.packet_lib import wave_ops
from steuermaschine_heinzel.plugin import plugin
from steuermaschine_heinzel.toolmap import ROWS

OLD = re.compile(r"\bhz-[a-z]")


def runtime_for(wave, core_repo_dir):  # noqa: F811
    home = wave["tmp"] / "home"
    stack = stack_of(plugin)
    table, _ = stack.validate({"heinzel": {"core_repo_dir": str(core_repo_dir), "lock_directory": str(home / ".cache"),
                                           "lock_history": str(home / "history.jsonl"), "lock_allow_full_log": str(home / "full.log")}}, Path("."))
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=True), stack, table,
                   _providers=fake_providers(commands=Subprocess(), clock=SystemClock()))


@pytest.fixture
def rt(wave, monkeypatch):  # noqa: F811
    (wave["tmp"] / "home/.cache").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(wave["tmp"] / "home"))  # a sandbox: the lock has its own files
    monkeypatch.setattr("steuermaschine.runlock.os.getloadavg", lambda: (0.0, 0.0, 0.0))  # a fixed load: the machine's does not decide
    return runtime_for(wave, wave["tmp"] / "shared-core")


@pytest.fixture
def gated_requests(monkeypatch):
    requests = []
    lock_of = packet.lock_of

    class RecordingLock:
        def run(self, request):
            requests.append(request)
            return lock_of().run(request)

    monkeypatch.setattr(packet, "lock_of", RecordingLock)
    return requests


def assert_collections_recorded(history, requests):
    assert len(history) == len(requests) == 2
    for entry, request in zip(history, requests, strict=True):
        assert Path(request.argv[2]).name == "collect_cases.py" and request.argv[3] == "collect"
        assert entry["key"] == RunLock.history_key(request.argv, request.cwd, request.environ)
        assert entry["cmd"] == " ".join(request.argv)[:200] and entry["exit"] == 0


def inputs(d, name, out):
    return [name, "--core-repo", str(d["repo"]), "--worktree", str(d[f"wt-{name}"]), "--out", str(d["root"] / out), "--review-root", str(d["root"]),
            "--selection", str(d[f"sel-{name}"]), "--junit", f"python={d[f'junit-{name}']}", "--members", f"member {name}"]


def preopen(rt, d, name="a", out=None, fmt=()):
    done = invoke(rt, [*fmt, "head", "packet", "preopen", *inputs(d, name, out or f"{name}-pre"), "--main", d["main"]])
    assert done.exit_code == 0, done
    return done


def test_preopen_in_text_names_the_prepare_and_collects_under_the_run_lock(rt, wave, gated_requests):  # noqa: F811
    done = preopen(rt, wave)
    out = wave["root"] / "a-pre"
    lines = done.stdout.splitlines()
    assert lines[0] == f"preopen packet {out / 'packet.json'}" and lines[1].startswith("sha256 ")
    assert "python: collected head tree == local focused JUnit; resulting tree identical" in lines
    assert shlex.split(lines[-1]) == ["sm", "head", "prepare", str(out / "packet.json"), "--core-repo", str(wave["wt-a"].resolve()), "--review-root",
                                      str(wave["root"].resolve())]
    review = (out / "REVIEW.md").read_text()
    assert review.startswith(f"# preopen pair: a head {wave['a'][:12]} onto {wave['main'][:12]} (builder-prepared by sm head packet; NOT the independent review)")
    assert "member a" in review and not OLD.search(review) and not OLD.search(done.stdout) and not OLD.search(done.stderr)
    assert (out / "verify.err").read_text().splitlines()[-1] == f"BYTE-EQUAL sha256 {lines[1].split()[1]}"
    assert (out / "python.pr-collected.xml").read_bytes() == (out / "python.main-collected.xml").read_bytes()
    assert '<testcase classname="tests.unit.a_test" name="test_two"' in (out / "python.main-collected.xml").read_text()
    record = json.loads((out / "wave.json").read_text())
    assert record["collector"].endswith("packet_lib/collect_cases.py") and record["kind"] == "preopen"
    history = [json.loads(line) for line in (wave["tmp"] / "home/history.jsonl").read_text().splitlines()]
    assert_collections_recorded(history, gated_requests)  # both trees, under the lock
    verified = invoke(rt, ["head", "manifest", "--verify", str(out / "packet.json"), "--core-repo", str(wave["repo"]), "--review-root", str(wave["root"])])
    assert verified.exit_code == 0 and verified.stdout.splitlines()[-1].startswith("BYTE-EQUAL")


def test_preopen_in_json_is_one_document_of_the_model(rt, wave):  # noqa: F811
    done = preopen(rt, wave, fmt=("--format", "json"))
    assert done.stdout.count("\n") == 1
    built = wave_ops.PacketBuilt.model_validate(json.loads(done.stdout))
    out = wave["root"] / "a-pre"
    assert built.kind == "preopen" and built.admissible and built.head == wave["a"] and built.main == wave["main"] and built.lanes == ["python"]
    assert built.directory == str(out.resolve()) and built.review_path == str(out.resolve() / "REVIEW.md") and built.prepare_command.startswith("sm head prepare ")
    assert built.packet_sha256 == json.loads((out / "wave.json").read_text())["packet_sha256"] and built.predicted_main == ""


def test_preopen_refuses_without_leaving_a_packet(rt, wave):  # noqa: F811
    (wave["wt-a"] / "src/heinzel/mod.py").write_text("x = 3\n")
    done = invoke(rt, ["head", "packet", "preopen", *inputs(wave, "a", "a-bad"), "--main", wave["main"]])
    assert done.exit_code == 1 and "is not clean" in done.stderr and done.stdout == ""
    assert not (wave["root"] / "a-bad/packet.json").exists() and not (wave["root"] / "a-bad/wave.json").exists()
    done = invoke(rt, ["--format", "json", "head", "packet", "preopen", *inputs(wave, "a", "a-short"), "--main", wave["main"][:12]])
    assert done.exit_code == 1 and "full 40-hex" in json.loads(done.stdout)["error"]


def test_a_failing_collection_refuses(rt, wave):  # noqa: F811
    done = invoke(rt, ["head", "packet", "preopen", *inputs(wave, "a", "a-py"), "--main", wave["main"], "--python", str(wave["tmp"] / "no-python")])
    assert done.exit_code == 1 and "collect python on the pr tree failed" in done.stderr and not (wave["root"] / "a-py/packet.json").exists()


def test_predict_then_bind_in_text_and_json(rt, wave):  # noqa: F811
    preopen(rt, wave)
    argv = ["head", "packet", "predict", *inputs(wave, "b", "b-pred"), "--after", str(wave["root"] / "a-pre")]
    done = invoke(rt, argv)
    assert done.exit_code == 0, done
    out = wave["root"] / "b-pred"
    record = json.loads((out / "wave.json").read_text())
    assert done.stdout.splitlines()[0] == f"predict packet {out / 'packet.json'}"
    assert done.stdout.splitlines()[-1] == f"PREDICTED only, not admissible. After the earlier heads merged: sm head packet bind {out.resolve()} --main REAL_MAIN_SHA"
    assert "prepare" not in done.stdout and "run `sm head packet bind`" in done.stderr
    assert record["kind"] == "predict" and _git(wave["repo"], "rev-parse", f"{record['predicted_main']}^") == wave["main"]
    argv[argv.index(str(wave["root"] / "b-pred"))] = str(wave["root"] / "b-json")
    predicted = wave_ops.PacketBuilt.model_validate(json.loads(invoke(rt, ["--format", "json", *argv]).stdout))
    assert not predicted.admissible and predicted.predicted_main == predicted.main == record["predicted_main"] and predicted.parent_main == wave["main"]
    assert predicted.prepare_command == ""

    actual = squash(wave, "a", wave["main"], "1700000500 +0000")
    done = invoke(rt, ["head", "packet", "bind", str(out), "--main", actual])
    assert done.exit_code == 0, done
    bound_dir = out / f"bound-{actual[:12]}"
    assert done.stdout.splitlines()[0] == f"bound packet {bound_dir / 'packet.json'}"
    assert shlex.split(done.stdout.splitlines()[-1])[:4] == ["sm", "head", "prepare", str(bound_dir / "packet.json")]
    assert "Bind to real main" in (bound_dir / "REVIEW.md").read_text() and not OLD.search((bound_dir / "REVIEW.md").read_text())
    bound = wave_ops.PacketBuilt.model_validate(json.loads(invoke(rt, ["--format", "json", "head", "packet", "bind", str(out), "--main", actual, "--out",
                                                                       str(wave["root"] / "b-bound")]).stdout))
    assert bound.kind == "bound" and bound.admissible and bound.main == actual and bound.directory == str((wave["root"] / "b-bound").resolve())
    assert bound.collection == [] and bound.predicted_main == record["predicted_main"]


def test_predict_refuses_the_shared_repository_and_bind_says_how_to_go_on(rt, wave):  # noqa: F811
    preopen(rt, wave)
    argv = ["head", "packet", "predict", *inputs(wave, "b", "b-pred"), "--after", str(wave["root"] / "a-pre")]
    done = invoke(runtime_for(wave, wave["repo"]), argv)  # the setting core_repo_dir names the shared repository
    assert done.exit_code == 1 and "must be a scratch clone" in done.stderr
    assert invoke(rt, argv).exit_code == 0
    done = invoke(rt, ["head", "packet", "bind", str(wave["root"] / "b-pred"), "--main", wave["b"]])
    assert done.exit_code == 1 and "re-prepare with `sm head packet preopen`" in done.stderr and not OLD.search(done.stderr)


def test_wave_checks_the_batch_and_says_the_batch_prepare_is_not_ported(rt, wave):  # noqa: F811
    preopen(rt, wave)
    preopen(rt, wave, name="b")
    dirs = [str(wave["root"] / "a-pre"), str(wave["root"] / "b-pre")]
    done = invoke(rt, ["head", "packet", "wave", *dirs])
    assert done.exit_code == 0, done
    lines = done.stdout.splitlines()
    assert lines[0] == f"one wave: 2 packets at main {wave['main'][:12]}, one review root, distinct heads, clean checkouts"
    assert lines[1].split() == [f"{dirs[0]}/packet.json", "checkout", str(wave["wt-a"].resolve())] and lines[-1].startswith("NOT PORTED YET: the one batch prepare")
    assert not any(line.startswith("sm ") for line in lines)  # nothing that looks runnable
    plan = wave_ops.WavePlan.model_validate(json.loads(invoke(rt, ["--format", "json", "head", "packet", "wave", *dirs, "--nonconsumers", dirs[0] + "/sel.json"]).stdout))
    assert plan.pairs == 2 and plan.main == wave["main"] and plan.packets == [f"{d}/packet.json" for d in dirs] and not plan.batch_prepare_ported
    assert plan.core_repos == [str(wave["wt-a"].resolve()), str(wave["wt-b"].resolve())] and plan.review_root == str(wave["root"].resolve())
    assert plan.nonconsumers == dirs[0] + "/sel.json"
    done = invoke(rt, ["head", "packet", "wave", dirs[0]])
    assert done.exit_code == 1 and "2..20" in done.stderr


def test_registrations_help_and_tool_map(rt):
    reg = {c.path: c for c in native.commands() if hasattr(c, "writes")}
    for path in ("head packet preopen", "head packet predict", "head packet bind", "head packet wave"):
        assert reg[path].writes is (path != "head packet wave") and "packet" in reg[path].help  # the builders write packet files
    assert {row.tool: row.path for row in ROWS}["hz-admit-wave"] == "head packet"
    done = invoke(rt, ["head", "packet", "--help"])
    assert done.exit_code == 0 and "Changes: writes packet files under the review root" in done.stdout
    for sub in ("preopen", "predict", "bind", "wave"):
        assert sub in done.stdout
