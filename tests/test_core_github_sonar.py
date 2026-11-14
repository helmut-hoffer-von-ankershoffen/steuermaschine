"""The Sonar quality provider against a fake HTTP function. No network."""

import json

from steuermaschine.github import QualityProvider
from steuermaschine.github.sonar import Sonar
from steuermaschine.testing import FakeClock

GATE = {"projectStatus": {"status": "ERROR", "conditions": [
    {"status": "OK", "metricKey": "new_reliability_rating", "comparator": "GT", "errorThreshold": "1", "actualValue": "1"},
    {"status": "ERROR", "metricKey": "new_duplicated_lines_density", "comparator": "GT", "errorThreshold": "3", "actualValue": "7.5"}]}}
MEASURES = {"component": {"key": "org_p", "measures": [{"metric": "new_sqale_debt_ratio", "periods": [{"index": 1, "value": "1.25"}]},
                                                        {"metric": "new_duplicated_lines_density", "period": {"value": "7.5"}}, {"metric": "other", "value": "x"}]}}
ISSUES = {"total": 3, "issues": [{"key": "k1", "rule": "python:S1192", "severity": "MAJOR", "type": "CODE_SMELL", "component": "org_p:src/a.py", "line": 12, "message": "Define a constant"},
                                 {"key": "k2", "rule": "python:S100", "severity": "MINOR", "component": "org_p:src/b.py", "message": "Rename"}]}


class Http:
    def __init__(self, **answers):
        self.answers, self.calls = answers, []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, headers, timeout))
        for part, answer in self.answers.items():
            if f"/api/{part.replace('_', '/', 1)}" in url:
                answer = answer.pop(0) if isinstance(answer, list) else answer
                return answer if isinstance(answer, tuple) else (200, json.dumps(answer))
        return 404, '{"errors":[{"msg":"not found"}]}'


def ok():
    return Http(qualitygates_project_status=GATE, measures_component=MEASURES, issues_search=ISSUES)


def test_report_has_gate_debt_ratio_duplication_and_open_issues():
    http = ok()
    s = Sonar("org_p", fetch=http, timeout=9)
    assert isinstance(s, QualityProvider)
    r = s.pr_quality(12, issues=2)
    assert (r.state, r.gate, r.debt_ratio, r.duplication, r.coverage, r.open_issues, r.issues_truncated) == ("ok", "failed", 1.25, 7.5, None, 3, True)
    assert [(c.metric, c.status, c.actual, c.threshold) for c in r.conditions][1] == ("new_duplicated_lines_density", "error", "7.5", "3")
    assert (r.issues[0].file, r.issues[0].line, r.issues[0].severity, r.issues[0].rule) == ("src/a.py", 12, "major", "python:S1192")
    assert r.url == "https://sonarcloud.io/dashboard?id=org_p&pullRequest=12" and r.exit_code == 0
    urls = [c[0] for c in http.calls]
    assert urls[0] == "https://sonarcloud.io/api/qualitygates/project_status?projectKey=org_p&pullRequest=12"
    assert "metricKeys=new_sqale_debt_ratio%2Cnew_duplicated_lines_density%2Cnew_coverage" in urls[1] and "resolved=false&ps=2" in urls[2]
    assert all(c[2] == 9 and "Authorization" not in c[1] for c in http.calls)
    text = "\n".join(r.render())
    assert "quality gate: failed" in text and "ERROR new_duplicated_lines_density: 7.5" in text and "debt ratio 1.25%" in text and "src/a.py:12" in text and "raise --issues" in text


def test_a_token_is_sent_only_when_given_and_the_host_can_be_another_one():
    http = ok()
    Sonar("org_p", host="https://sonar.example/", fetch=http, token="t0").pr_quality(1)
    assert http.calls[0][0].startswith("https://sonar.example/api/") and http.calls[0][1]["Authorization"] == "Bearer t0"


def test_one_retry_on_no_answer_and_on_5xx_then_unknown_with_the_reason():
    clock = FakeClock()
    http = Http(qualitygates_project_status=[(0, "timed out"), GATE], measures_component=[(503, ""), MEASURES], issues_search=(500, "boom"))
    r = Sonar("org_p", fetch=http, clock=clock).pr_quality(5)
    assert (r.gate, r.debt_ratio, r.open_issues) == ("failed", 1.25, None) and r.state == "unknown: issues: HTTP 500" and r.exit_code == 1
    assert len(http.calls) == 6
    r = Sonar("org_p", fetch=Http(qualitygates_project_status=(0, "name resolution failed")), clock=clock).pr_quality(5)
    assert r.gate.startswith("unknown: no answer from https://sonarcloud.io: name resolution failed")


def test_not_found_wrong_shape_and_no_project_key_are_results_not_exceptions():
    r = Sonar("nope", fetch=Http()).pr_quality(1)
    assert r.gate == "unknown: HTTP 404: not found" and r.state.startswith("unknown: gate: HTTP 404") and r.render()
    r = Sonar("org_p", fetch=Http(qualitygates_project_status=(200, "<html>"), measures_component=(200, "[]"), issues_search={"paging": {"total": 0}})).pr_quality(1)
    assert r.gate.startswith("unknown: ") and r.open_issues == 0 and not r.issues_truncated
    http = Http()
    r = Sonar("", fetch=http).pr_quality(1)
    assert r.state.startswith("unknown: no Sonar project") and http.calls == []


def test_gate_words():
    for raw, word in (("OK", "passed"), ("NONE", "none"), ("WARN", "failed")):
        http = Http(qualitygates_project_status={"projectStatus": {"status": raw}}, measures_component={"component": {}}, issues_search={"total": 0, "issues": []})
        r = Sonar("org_p", fetch=http).pr_quality(1, issues=0)
        assert r.gate == word and r.state == "ok" and r.issues == []
    http = Http(qualitygates_project_status={"projectStatus": {"status": "ODD"}}, measures_component={"component": {}}, issues_search={"total": 0})
    assert Sonar("org_p", fetch=http).pr_quality(1).gate.startswith("unknown: gate status")


def test_urllib_fetch_turns_transport_errors_into_status_zero(monkeypatch):
    import urllib.error

    from steuermaschine.github import sonar

    def boom(*args, **kwargs):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(sonar.urllib.request, "urlopen", boom)
    assert sonar.urllib_fetch("https://x.invalid/api", {}, 1) == (0, "no route")

    def http_error(*args, **kwargs):
        import io
        raise urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"errors":[]}'))

    monkeypatch.setattr(sonar.urllib.request, "urlopen", http_error)
    assert sonar.urllib_fetch("https://x.invalid/api", {}, 1) == (404, '{"errors":[]}')

    class Resp:
        status = 200

        def read(self):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(sonar.urllib.request, "urlopen", lambda *a, **k: Resp())
    assert sonar.urllib_fetch("https://x.invalid/api", {}, 1) == (200, "{}")
