"""ci classify, native: the one command that may call the outside classifier. The service is replaced by a function;
no test here starts a process or reads the network."""

from pathlib import Path

from steuermaschine_heinzel.native import ci_classify
from steuermaschine_heinzel.native.ci_classify_lib import jev
from steuermaschine_heinzel.plugin import plugin

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke

RUN = 42398276348
LOG = (Path(__file__).parent / "fixtures" / "ci" / f"run-{RUN}.log").read_text()
KEY = "apikey_" + "ab12cd34" * 4 + "_s3cr3tS3cr3tVALUE"
ANSWER = {"answers": {"cause": {"choice": "test-assertion", "confidence": 0.93, "probabilities": {"test-assertion": 0.93}}}, "usage": {"input_tokens": 10}}


def runtime_for(tmp_path, commands, reviewed=True, **settings):
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": {"legacy_state_dir": str(tmp_path / "state"), "ci_classifier_dir": str(tmp_path / "jev"), **settings}}, Path("."))
    project = Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"], allow_unreviewed=reviewed)
    return Runtime(project, stack, validated, _providers=fake_providers(commands=commands))


def service(monkeypatch, reply=(200, ANSWER, None)):
    posts = []
    monkeypatch.setattr(jev, "_http", lambda method, path, key, body, timeout: posts.append((method, path, key, body)) or reply)
    monkeypatch.setattr(jev, "_sleep", lambda seconds: None)
    return posts


def test_off_by_default_nothing_is_sent_and_no_key_is_read(tmp_path, monkeypatch):
    posts, fake = service(monkeypatch), FakeGh().on("--log-failed", LOG)
    out = invoke(runtime_for(tmp_path, fake), ["ci", "classify", str(RUN)])
    assert out.exit_code == 1 and "the classifier is off (setting ci_classifier)" in out.stderr
    assert posts == [] and fake.calls == [] and not (tmp_path / "jev").exists()


def test_switched_on_it_sends_the_condensed_log_once_and_reports_the_budget(tmp_path, monkeypatch):
    posts, fake = service(monkeypatch), FakeGh().on("--log-failed", LOG)
    out = invoke(runtime_for(tmp_path, fake, ci_classifier=True, ci_classifier_key=KEY), ["ci", "classify", str(RUN)])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "classifier: test-assertion p=0.93" and lines[2] == "budget today: 1 of 2000 calls, " + lines[2].split(", ")[1]
    (method, path, key, body), = posts
    assert (method, path, key) == ("POST", "/v1/systemone", KEY) and lines[1] == f"sent {len(body['state'])} characters of the condensed public log of run {RUN} to api.typesafe.ai"
    assert "FAILED tests/e2e/scheduled_connected_test.py" in body["state"] and len(body["state"]) < len(LOG)
    assert KEY not in out.stdout and KEY not in out.stderr and [argv[0] for argv in fake.calls] == ["gh"]  # the key came from the settings: no keychain read
    report = ci_classify.Classification.model_validate_json(invoke(runtime_for(tmp_path, fake, ci_classifier=True, ci_classifier_key=KEY), ["--format", "json", "ci", "classify", str(RUN)]).stdout)
    assert report.label == "test-assertion" and report.probability == 0.93 and report.calls_today == 2 and report.service == "api.typesafe.ai"


def test_the_keychain_is_asked_only_here_and_only_without_a_configured_key(tmp_path, monkeypatch):
    posts, fake = service(monkeypatch), FakeGh().on("--log-failed", LOG).on("find-generic-password -s item -w", KEY + "\n")
    out = invoke(runtime_for(tmp_path, fake, ci_classifier=True, ci_classifier_keychain_item="item"), ["ci", "classify", str(RUN)])
    assert out.exit_code == 0 and posts[0][2] == KEY and [argv[0] for argv in fake.calls] == ["gh", "security"] and KEY not in out.stdout + out.stderr


def test_no_confident_answer_is_said_plainly(tmp_path, monkeypatch):
    service(monkeypatch, (500, None, None))
    out = invoke(runtime_for(tmp_path, FakeGh().on("--log-failed", LOG), ci_classifier=True, ci_classifier_key=KEY), ["ci", "classify", str(RUN)])
    assert out.exit_code == 0 and out.stdout.splitlines()[0] == "classifier: no confident class (or no answer)"


def test_refusals_send_nothing(tmp_path, monkeypatch):
    posts = service(monkeypatch)
    on = {"ci_classifier": True, "ci_classifier_key": KEY}
    out = invoke(runtime_for(tmp_path, FakeGh(), ci_classifier=True, ci_classifier_key=KEY, ci_classifier_dir=None), ["ci", "classify", str(RUN)])
    assert out.exit_code == 1 and "ci_classifier_dir is not set" in out.stderr
    out = invoke(runtime_for(tmp_path, FakeGh(), **on), ["ci", "classify", "795"])
    assert out.exit_code == 1 and "give a run id" in out.stderr
    out = invoke(runtime_for(tmp_path, FakeGh(), repo="someone/private", **on), ["ci", "classify", str(RUN)])
    assert out.exit_code == 1 and "not on the allowlist" in out.stderr
    for answer, word in ((502, "no failed-step log to classify: "), ("\n", "no failed-step log to classify")):
        out = invoke(runtime_for(tmp_path, FakeGh().on("--log-failed", answer), **on), ["ci", "classify", str(RUN)])
        assert out.exit_code == 1 and word in out.stderr
    (tmp_path / "jev").mkdir(exist_ok=True)
    (tmp_path / "jev" / "OFF").write_text("")
    fake = FakeGh().on("--log-failed", LOG)
    out = invoke(runtime_for(tmp_path, fake, **on), ["ci", "classify", str(RUN)])
    assert out.exit_code == 1 and "kill switch file" in out.stderr and fake.calls == [] and posts == []
