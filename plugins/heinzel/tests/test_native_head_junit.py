"""bf3i: `sm head junit` runs the whole Vitest suite once as JUnit when the head's selection is web/ALL, under the run
lock with the full-suite refusal waived and logged; any other selection is refused."""

import json
from pathlib import Path

import pytest

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.runlock import Outcome
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import junit
from steuermaschine_heinzel.plugin import plugin


@pytest.fixture
def rt(tmp_path):
    stack = stack_of(plugin)
    settings, _ = stack.validate({"heinzel": {"lock_allow_full_log": str(tmp_path / "allow-full.log")}}, tmp_path)
    return Runtime(Project(name="t", plugin="heinzel", root=tmp_path, plugins=["heinzel"], allow_unreviewed=True), stack, settings,
                   _providers=fake_providers())


@pytest.fixture
def head(tmp_path):
    bin_dir = tmp_path / "wt" / "web" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vitest").write_text("")
    return tmp_path / "wt"


def selection(tmp_path: Path, web) -> Path:
    path = tmp_path / "sel.json"
    path.write_text(json.dumps({"python": ["tests/unit/x_test.py"], "web": web}))
    return path


@pytest.fixture
def ran(monkeypatch):
    seen = []

    def gated(request, code=0):
        seen.append(request)
        out = next(arg.split("=", 1)[1] for arg in request.argv if arg.startswith("--outputFile.junit="))
        Path(out).write_text('<testsuites><testsuite><testcase name="a"/><testcase name="b"/></testsuite></testsuites>')
        return Outcome("ran", seen_code[0])

    seen_code = [0]
    monkeypatch.setattr(junit, "gated", gated)
    return seen, seen_code


def test_a_web_all_head_gets_one_full_junit_run_logged_as_waived(rt, head, tmp_path, ran):
    seen, _ = ran
    out = tmp_path / "packets" / "web.xml"
    done = invoke(rt, ["--format", "json", "head", "junit", str(head), "--sel", str(selection(tmp_path, ["web/ALL"])), "--output", str(out)])
    doc = junit.JunitRun.model_validate(json.loads(done.stdout))
    assert done.exit_code == 0 and doc.cases == 2 and doc.output == str(out.resolve()), done
    (request,) = seen
    assert request.argv[0].endswith("web/node_modules/.bin/vitest") and request.argv[1:5] == ["run", "--maxWorkers=2", "--reporter=default", "--reporter=junit"]
    assert request.cwd == str(head.resolve() / "web")
    log = (tmp_path / "allow-full.log").read_text()
    assert "web/ALL selection" in log and str(head.resolve()) in log
    text = invoke(rt, ["head", "junit", str(head), "--sel", str(selection(tmp_path, ["ALL"])), "--output", str(out)]).stdout
    assert "2 Vitest cases (the whole suite, web/ALL)" in text and f"--junit web={out.resolve()}" in text


def test_a_red_run_is_not_a_junit_of_the_head(rt, head, tmp_path, ran):
    _, code = ran
    code[0] = 1
    done = invoke(rt, ["head", "junit", str(head), "--sel", str(selection(tmp_path, ["web/ALL"])), "--output", str(tmp_path / "w.xml")])
    assert done.exit_code == 1 and "exited 1" in done.stdout


def test_any_other_selection_or_a_missing_install_is_refused_and_nothing_runs(rt, head, tmp_path, ran):
    seen, _ = ran
    focused = invoke(rt, ["head", "junit", str(head), "--sel", str(selection(tmp_path, ["web/src/a.test.ts"])), "--output", str(tmp_path / "w.xml")])
    assert focused.exit_code == 2 and "run its files focused instead" in focused.stderr
    bad = tmp_path / "bad.json"
    bad.write_text("{")
    assert invoke(rt, ["head", "junit", str(head), "--sel", str(bad), "--output", str(tmp_path / "w.xml")]).exit_code == 2
    bare = tmp_path / "bare"
    bare.mkdir()
    missing = invoke(rt, ["head", "junit", str(bare), "--sel", str(selection(tmp_path, ["web/ALL"])), "--output", str(tmp_path / "w.xml")])
    assert missing.exit_code == 2 and "install the web dependencies" in missing.stderr
    assert seen == [] and not (tmp_path / "allow-full.log").exists()


@pytest.mark.parametrize("status, code, needle", [("gave_up", 75, "did not admit"), ("refused", 1, "refused the full Vitest run")])
def test_the_run_lock_says_wait_or_no(rt, head, tmp_path, monkeypatch, status, code, needle):
    monkeypatch.setattr(junit, "gated", lambda request: Outcome(status, 75 if status == "gave_up" else 2, reason="busy"))
    done = invoke(rt, ["head", "junit", str(head), "--sel", str(selection(tmp_path, ["web/ALL"])), "--output", str(tmp_path / "w.xml")])
    assert done.exit_code == code and needle in done.stderr


def test_an_unreadable_junit_counts_no_cases(tmp_path):
    (tmp_path / "x.xml").write_text("<oops")
    assert junit.count_cases(tmp_path / "x.xml") == 0 and junit.count_cases(tmp_path / "none.xml") == 0


def test_the_run_goes_through_the_run_lock_with_the_full_suite_waived(monkeypatch):
    seen = {}

    class Lock:
        def run(self, request):
            seen["request"] = request
            return Outcome("ran", 0)

    monkeypatch.setattr(junit.lock, "lock_of", lambda allow_full=False: seen.update(allow_full=allow_full) or Lock())
    request = junit.Request(argv=["vitest", "run"])
    assert junit.gated(request).exit_code == 0 and seen == {"allow_full": True, "request": request}
