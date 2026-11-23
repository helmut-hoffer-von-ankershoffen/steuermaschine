"""status, native. Parity fixture fixtures/status/status-20261112.json: recorded 2026-11-12 23:05 with
`.venv/bin/python plugins/heinzel/tests/record_status_fixtures.py 20261112` (real read-only GitHub, Sonar and badge
answers, and what the old status tool's rows(), render(), slot_line() and the old gate audit print for them).
No test here starts a process or reads the network."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.github.client import Gh
from steuermaschine.github.sonar import Sonar
from steuermaschine.github.testing import FakeGh
from steuermaschine.plugins import stack_of
from steuermaschine.testing import fake_providers, invoke
from steuermaschine_heinzel.native import status as status_mod
from steuermaschine_heinzel.native.status_lib import gate, table
from steuermaschine_heinzel.plugin import plugin

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "status" / "status-20261112.json").read_text())
OLD = re.compile(r"(?<![\w./-])hz-[a-z]")
NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def recorded() -> FakeGh:
    fake = FakeGh()
    for key, answer in FIXTURE["gh"].items():
        fake.on(f"-F repo={key.split('repo=')[1]}" if key.startswith("graphql") else key, answer if answer is not None else 404)
    return fake


class Web:
    """The injected HTTP GET: the recorded Sonar and badge answers by URL, and every URL that was asked."""

    def __init__(self, answers=None):
        self.answers, self.asked = dict(FIXTURE["curl"] if answers is None else answers), []

    def __call__(self, url, headers, timeout):
        self.asked.append(url)
        answer = self.answers.get(unquote(url))
        return (404, "") if answer is None else (200, answer if isinstance(answer, str) else json.dumps(answer))


def runtime_for(tmp_path, commands, web=None, **settings):
    actions = tmp_path / "actions.toml"
    actions.write_text("\n".join(f'"{k}" = {{ action = {json.dumps(v["action"])}, owner = "{v["owner"]}" }}' for k, v in FIXTURE["actions"].items()))
    table_settings = {"legacy_state_dir": str(tmp_path / "state"), "go_pr_actions_file": str(actions), **settings}
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": table_settings}, Path("."))
    providers = fake_providers(commands=commands, extra={"http_fetch": web or Web()})
    providers.clock.t = FIXTURE["now"]
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, validated, _providers=providers)


def strip(text: str) -> list[str]:
    lines = text.split("\n")
    while lines and not lines[-1]:
        lines.pop()
    return lines


# ------------------------------------------------------------------ parity with the recorded old output
def test_text_is_the_old_table_line_for_line(tmp_path):
    fake = recorded()
    out = invoke(runtime_for(tmp_path, fake), ["status"])
    assert out.exit_code == 0, out
    assert out.stdout.splitlines() == strip(FIXTURE["old_text"])
    assert not fake.writes() and not OLD.search(out.stdout)
    assert {argv[0] for argv in fake.calls} == {"gh"}


def test_markdown_and_stale_are_the_old_ones(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded()), ["status", "--md"])
    assert out.stdout.splitlines() == strip(FIXTURE["old_markdown"])
    web = Web()
    out = invoke(runtime_for(tmp_path, recorded(), web), ["status", "--stale"])
    assert out.stdout.splitlines() == FIXTURE["old_stale"]
    assert web.asked == []  # the stale view reads no gate


def test_json_is_one_document_of_the_model(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded()), ["--format", "json", "status"])
    assert out.exit_code == 0, out
    report = status_mod.Status.model_validate_json(out.stdout)
    assert [g.repo for g in report.gate] == ["heinzel", "heinzel-plugins", "heinzel-acme"] and report.gate[0].required == ["merge-gate", "coverage-floor"]
    assert report.slots and report.slots.source == "live" and report.slots.heads_left == 43 and report.slots.used_bytes["FOCUSED_CI_PLANS"] == 47970
    assert {p.group for p in report.prs} == {"ready", "needs work"} and report.no_owner == ["#797", "#784"] and report.read_only
    assert "x" * 100 not in out.stdout  # a variable value never leaves the probe


def test_check_exits_one_and_names_the_pull_requests_without_owner(tmp_path):
    out = invoke(runtime_for(tmp_path, recorded()), ["status", "--check"])
    assert out.exit_code == 1 and out.stdout.splitlines()[-1] == "NO OWNER: #797, #784"


# ------------------------------------------------------------------ degraded reads
def test_a_repository_that_cannot_be_listed_and_unreadable_slots_are_named_not_fatal(tmp_path):
    fake = recorded()
    fake.on("-F repo=heinzel-acme", 502)
    fake.on("actions/variables", 502)
    out = invoke(runtime_for(tmp_path, fake, status_gate_notes={"heinzel-private": "private, no CI: nothing to gate"}), ["status"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert "heinzel-private: private, no CI: nothing to gate" in lines and not any(line.startswith("Focused slots") for line in lines)
    assert any(line.startswith("UNKNOWN  could not list heinzel-acme: ") for line in lines) and any(line.startswith("UNKNOWN  focused slots: ") for line in lines)
    assert not any("acme #" in line for line in lines)
    fake.on("repos/helmut-hoffer-von-ankershoffen/heinzel/rulesets", 502)
    out = invoke(runtime_for(tmp_path, fake), ["status"])
    assert "  <<< RULESET UNREAD" in out.stdout.splitlines()[2] and any(line.startswith("UNKNOWN  ruleset of heinzel: ") for line in out.stdout.splitlines())


def test_slots_fall_back_to_the_last_hourly_read(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "status-slots.json").write_text(json.dumps({"at": 1, "sizes": {"FOCUSED_CI_PLANS": 1000}}))
    fake = recorded()
    fake.on("actions/variables", 502)
    out = invoke(runtime_for(tmp_path, fake), ["--format", "json", "status"])
    report = status_mod.Status.model_validate_json(out.stdout)
    assert report.slots and report.slots.source == "cache" and report.slots.used_bytes["FOCUSED_CI_PLANS"] == 1000 and report.unknown == []


def test_an_unreadable_action_table_is_named(tmp_path):
    rt = runtime_for(tmp_path, recorded())
    (tmp_path / "actions.toml").write_text("not toml =")
    out = invoke(rt, ["status"])
    assert out.exit_code == 0 and any(line.startswith("UNKNOWN  the action table is unreadable") for line in out.stdout.splitlines())


def test_an_answer_without_pull_requests_is_unknown():
    fake = FakeGh().on("graphql", {"data": {"repository": None}})
    assert status_mod.fetch_prs(Gh(fake), "o", "r") == ([], "could not list r: no pull requests in the answer")


# ------------------------------------------------------------------ the gate
def test_gate_marks_what_to_act_on():
    web = Web({"https://sonarcloud.io/api/qualitygates/project_status?projectKey=o_r": {"projectStatus": {"status": "ERROR"}},
               "https://sonarcloud.io/api/measures/component?component=o_r&metricKeys=reliability_rating,security_rating,sqale_rating":
                   {"component": {"measures": [{"metric": "reliability_rating", "value": "1.0"}, {"metric": "security_rating", "value": "3.0"}]}},
               gate.BADGE.format(repo="o/r"): {"message": "71%", "color": "red"}})
    fake = FakeGh()
    fake.on("repos/o/r/rulesets", [{"id": 7}, {"id": 8}])
    fake.on("repos/o/r/rulesets/7", {"enforcement": "disabled", "rules": [{"type": "required_status_checks"}]})
    fake.on("repos/o/r/rulesets/8", {"enforcement": "active", "bypass_actors": [{"actor_id": 5}],
                                     "rules": [{"type": "deletion"}, {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "merge-gate"}]}}]})
    row = gate.gate_row(Sonar("o_r", fetch=web), web, Gh(fake), "o", "r")
    assert row["marks"] == ["NOT A", "RED", "BYPASS"] and row["required"] == ["merge-gate"] and row["not_yet"] == ["coverage-floor"]
    assert gate.gate_line(row) == ("r: gate ERROR, reliability A security C maintainability ?; coverage 71% red; required: merge-gate; "
                                   "not yet: coverage-floor  <<< NOT A, RED, BYPASS")


def test_gate_reads_current_required_checks_without_legacy_policy(monkeypatch):
    monkeypatch.setattr(gate.core_facts, "api_checks", lambda: ())
    fake, web = FakeGh(), Web({})
    checks = [{"context": "CI OK"}, {"context": "SonarCloud Code Analysis"}]
    fake.on("repos/o/r/rulesets", [{"id": 8}])
    fake.on("repos/o/r/rulesets/8", {"enforcement": "active", "rules": [
        {"type": "required_status_checks", "parameters": {"required_status_checks": checks}},
    ]})
    row = gate.gate_row(Sonar("o_r", fetch=web), web, Gh(fake), "o", "r")
    assert row["required"] == ["CI OK", "SonarCloud Code Analysis"]
    assert row["not_yet"] == []

def test_a_ruleset_that_could_not_be_read_is_not_reported_as_missing():
    fake, web = FakeGh().on("rulesets", 502), Web({})
    row = gate.gate_row(Sonar("o_r", fetch=web), web, Gh(fake), "o", "r")
    assert row["marks"] == ["NOT A", "RED", "RULESET UNREAD"] and row["unknown"].startswith("ruleset of r: ")


def test_gate_without_answers_never_passes():
    fake, web = FakeGh().on("rulesets", 404), Web({})
    row = gate.gate_row(Sonar("o_r", fetch=web), web, Gh(fake), "o", "r")
    assert row["sonar"] == "Sonar UNAVAILABLE" and row["coverage"] == "coverage UNKNOWN" and row["marks"] == ["NOT A", "RED", "NO RULESET"]
    assert gate.gate_line(row).endswith("required: neither; not yet: merge-gate, coverage-floor  <<< NOT A, RED, NO RULESET")
    assert gate.badge(Web({gate.BADGE.format(repo="o/r"): "not json"}), "o/r") is None


def test_gate_checks_are_advisory_until_main_requires_them():
    rules = [{"type": "deletion"}, {"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "merge-gate"}, {"context": "CI OK"}]}}]
    ruled = gate.advisory_checks_from(rules)
    assert "review" in ruled and "coverage-floor" not in ruled and "Sonar" not in ruled and "merge-gate" not in ruled  # the rules waive nothing
    unread = gate.advisory_checks_from(None)  # unreadable rules: never a looser gate
    assert "review" in unread and "coverage-floor" not in unread and "merge-gate" not in unread


# ------------------------------------------------------------------ the table (the assertions of the old tool's tests)
def pr(number=1, **over):
    base = {"number": number, "isDraft": False, "mergeStateStatus": "CLEAN", "title": "feat(x): a title", "body": "", "updatedAt": "2026-11-12T11:00:00Z",
            "labels": [], "commits": [{"committedDate": "2026-11-12T11:30:00Z"}], "statusCheckRollup": []}
    return {**base, **over}


def test_summary_takes_the_first_sentence_else_the_title_without_prefix():
    assert table.summary("<!-- hidden -->\n# Heading\n\nThis change makes the queue faster. More text.", "fix: x") == "This change makes the queue faster"
    assert table.summary("short\n| a | b |", "feat(web)!: the title") == "the title"
    assert table.summary("- " + "word " * 30, "t").endswith("…") and len(table.summary("- " + "word " * 30, "t")) == 70
    assert table.summary("Tiny words. Then more of them here.", "fix: fallback") == "fallback"


def test_groups_and_failing_checks():
    red = [{"name": "CI OK", "conclusion": "FAILURE"}, {"name": "review", "conclusion": "FAILURE"}, {"name": "Focused web", "conclusion": "FAILURE"}]
    assert table.failing(red) == "CI OK" and table.failing(red[1:]) == ""
    assert table.group_of(pr(isDraft=True), False) == "draft" and table.group_of(pr(), True) == "held"
    assert table.group_of(pr(labels=[{"name": "do-not-merge"}]), False) == "held"
    assert table.group_of(pr(statusCheckRollup=red[1:]), False) == "ready" and table.group_of(pr(statusCheckRollup=red), False) == "needs work"
    assert table.group_of(pr(mergeStateStatus="BLOCKED"), False) == "needs work"
    gate_red = [{"name": "coverage-floor", "conclusion": "FAILURE"}]
    assert table.group_of(pr(statusCheckRollup=gate_red), False, {"review", "coverage-floor"}) == "ready"


def test_rows_ages_owners_and_keys_per_repository():
    prs = {"core": [pr(390), pr(391, commits=[])], "plugins": [pr(38, commits=[{"committedDate": "2026-11-09T12:00:00Z"}], updatedAt="2026-11-12T01:00:00Z")]}
    actions = {"390": {"action": "merge", "owner": "me"}, "plugins #38": {"action": "wait", "owner": "you", "hold": True}, "38": {"owner": "wrong"}}
    rows = table.rows(prs, actions, NOW, {"core": "", "plugins": "plugins "})
    assert [(r["pr"], r["owner"], r["age"], r["group"]) for r in rows] == [("#390", "me", "30m", "ready"), ("#391", "NO OWNER", "?", "ready"), ("plugins #38", "you", "3d", "held")]
    assert [r["pr"] for r in table.stale(rows, NOW)] == ["plugins #38"]
    assert table.age("2026-11-12T02:00:00Z", NOW) == "10h" and table.age("2026-11-12T13:00:00Z", NOW) == "0m"
    text = table.render(rows, True)
    assert "**Ready** (2)" in text and "| plugins #38 | a title | CLEAN |  | 3d | wait | you |" in text


def test_slot_line_counts_free_bytes_and_heads():
    line = table.slot_line({"FOCUSED_CI_PLANS": 49152, "FOCUSED_CI_PLANS_V2_0": 40000, "FOCUSED_CI_PLANS_V2_1": 40000, "FOCUSED_CI_PLANS_V2_2": 40000, "FOCUSED_CI_PLANS_V2_3": 38100})
    assert line.endswith("V2_3 38100/40000; 1900 B free, ~1 heads left (at ~1.9 KB per head)")
