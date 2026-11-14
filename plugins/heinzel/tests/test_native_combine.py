"""head combine status | plan, native. The pure tests moved with the code from the old combine tool's tests
(test_areas_and_solo_classes, test_member_subject_fits_72_and_keeps_type, test_plan_selection_rules: assertions kept,
imports renamed); the command tests run the command tree on a scripted `gh pr list`. No process, no network."""

import json
from pathlib import Path

from steuermaschine.app import Runtime
from steuermaschine.config import Project
from steuermaschine.plugins import stack_of
from steuermaschine.protocols import Result
from steuermaschine.testing import FakeCommands, fake_providers, invoke
from steuermaschine_heinzel.native import combine as combine_mod
from steuermaschine_heinzel.native.combine_lib import combine as hc
from steuermaschine_heinzel.plugin import plugin

NOW = 1762589400.0  # 2026-11-08 08:10Z


# ------------------------------------------------------------- moved: pure
def test_areas_and_solo_classes():
    assert hc.file_area("web/src/App.tsx") == "web" and hc.file_area("src/heinzel/x.py") == "system"
    assert hc.file_area(".github/workflows/ci.yml") == "tools" and hc.file_area("docs/a.md") == "neutral"
    assert hc.file_area("plugins/heinzel-clock/x.py", plugins=True) == "plugin:heinzel-clock"
    assert hc.member_area(["web/a.ts", "docs/x.md"]) == "web" and hc.member_area(["web/a.ts", "src/b.py"]) == "system"
    assert hc.solo_file("src/heinzel/heads/cli/_update.py") and hc.solo_file("src/heinzel/heads/api/_auth.py")
    assert not hc.solo_file("src/heinzel/modules/heinzel/_turn_authority_fence.py") and not hc.solo_file("docs/install.md")
    assert ".github/workflows/ci.yml" in hc.PINNED


def test_member_subject_fits_72_and_keeps_type():
    long = "feat(web): " + "very long summary words " * 6
    s = hc.member_subject(long, 1234)
    assert len(s) <= 72 and s.startswith("feat(web): very") and s.endswith(" (#1234)")
    assert hc.member_subject("fix(cli): short (#9)", 9) == "fix(cli): short (#9)"



def test_plan_selection_rules():
    def m(n, files, **kw):
        d = {"number": n, "title": f"feat(web): m{n}", "headRefName": f"feat/m{n}", "headRefOid": "s", "isDraft": True,
             "labels": [{"name": "combine:web"}], "files": [{"path": f, "additions": 5} for f in files],
             "createdAt": f"2026-11-08T08:{n:02d}:00Z", **kw}
        return hc.Member.from_gh(d)

    ms = [m(1, ["web/a.ts"]), m(2, ["web/b.ts"], isDraft=False), m(3, [".github/workflows/ci.yml", "web/c.ts"]),
          m(4, ["web/d.ts"], statusCheckRollup=[{"conclusion": "FAILURE"}]), m(5, ["web/e.ts"], baseRefName="feat/m7"),
          m(7, ["web/f.ts"]), m(8, ["web/a.ts", "web/src/api/schema.d.ts"]), m(9, ["web/g.ts"], labels=[]),
          m(10, ["web/x.ts"], baseRefName="feat/gone"), m(11, ["src/heinzel/heads/cli/_update.py", "web/u.ts"], labels=[{"name": "combine:web"}]),
          m(12, ["src/heinzel/core.py"], labels=[{"name": "combine:system"}])]
    p = hc.plan(ms, "web", inflight=[{"number": 700, "files": ["web/f.ts", "web/src/api/schema.d.ts"]}])
    assert [x.number for x in p.members] == [1, 7, 5, 8]  # stacked base 7 before 5; 8 overlaps 1, same combine
    why = {x.number: r for x, r in p.refused}
    assert "not a draft" in why[2] and "pinned" in why[3] and "failing" in why[4] and "label" in why[9]
    assert "stacked on feat/gone" in why[10] and "solo class" in why[11] and 12 not in why
    assert p.after == 700 and p.overlap == ["web/f.ts"]  # generated schema.d.ts does not count
    big = [m(20 + i, [f"web/z{i}.ts"]) for i in range(8)]
    p = hc.plan(big, "web")
    assert len(p.members) == 6 and len(p.deferred) == 2


# ------------------------------------------------------------- fixture repo



# ------------------------------------------------------------- the commands
def pr(n, files, **kw):
    return {"number": n, "title": f"feat(web): m{n}", "headRefName": f"feat/m{n}", "headRefOid": f"{n:040d}", "baseRefName": "main", "isDraft": True,
            "labels": [{"name": "combine:web"}], "mergeable": "MERGEABLE", "files": [{"path": f, "additions": 5, "deletions": 1} for f in files],
            "statusCheckRollup": [], "createdAt": f"2026-11-08T0{n % 8}:00:00Z", "body": "", **kw}


def listing(prs) -> FakeCommands:
    c = FakeCommands()
    c.on(["gh", "pr", "list"], Result(0, json.dumps(prs)))
    return c


def runtime_for(commands, **settings):
    stack = stack_of(plugin)
    validated, _ = stack.validate({"heinzel": {"repo": "o/core", **settings}}, Path("."))
    providers = fake_providers(commands=commands)
    providers.clock.t = NOW
    return Runtime(Project(name="t", plugin="heinzel", root=Path("."), plugins=["heinzel"]), stack, validated, _providers=providers)


INFLIGHT = {"number": 700, "title": "feat(web): a, b (combine of 2)", "headRefName": "combine/web-0800", "isDraft": False,
            "files": [{"path": "web/z.ts"}], "body": "x\n<!-- hz-combine area=web branch=combine/web-0800 base=abc after=- members=1:aa,2:bb -->"}


def test_status_in_text_names_the_due_area_and_the_cap():
    prs = [pr(n, [f"web/s{n}.ts"]) for n in range(1, 5)] + [pr(9, ["src/x.py"], labels=[]), INFLIGHT]
    commands = listing(prs)
    out = invoke(runtime_for(commands), ["head", "combine", "status"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "combines in flight 1/4 (hard cap 8): ok"
    assert lines[1] == "  #700 combine/web-0800 area web, 2 member(s)"
    assert "  system: 0 ready of 1 draft, oldest 0 min" in lines
    assert any(line.startswith("  web: 4 ready of 4 draft") and line.endswith("-> sm head combine build --area web") for line in lines)
    assert commands.calls == [["gh", "pr", "list", "-R", "o/core", "--state", "open", "--limit", "100", "--json", hc.PR_FIELDS]]
    assert "hz-" not in out.stdout


def test_status_in_json_validates():
    out = invoke(runtime_for(listing([pr(1, ["web/a.ts"]), INFLIGHT])), ["--format", "json", "head", "combine", "status"])
    doc = combine_mod.CombineStatus.model_validate_json(out.stdout)
    assert doc.inflight[0].members == [1, 2] and doc.drafts[0].number == 1 and doc.drafts[0].reason == "" and doc.repo == "o/core"


def test_plan_in_text_and_json():
    prs = [pr(1, ["web/a.ts"]), pr(2, ["web/b.ts"], isDraft=False), pr(3, ["web/c.ts"], mergeable="CONFLICTING")]
    out = invoke(runtime_for(listing(prs)), ["head", "combine", "plan"])
    assert out.exit_code == 0, out
    lines = out.stdout.splitlines()
    assert lines[0] == "area web: 1 member(s), 6 lines, 1 files; combines in flight 0/4 (hard cap 8): ok"
    assert lines[1].startswith("  + #1 feat(web): m1 (#1)")
    assert "  - #3 feat(web): m3: conflicts with main: merge origin/main into it" in lines and "  - not drafts (own CI, ship alone): #2" in lines
    assert lines[-1].startswith("next: build the combine (not ported yet") and "hz-" not in out.stdout
    out = invoke(runtime_for(listing(prs)), ["--format", "json", "head", "combine", "plan", "--without", "3"])
    doc = combine_mod.CombinePlan.model_validate_json(out.stdout)
    assert [m.number for m in doc.members] == [1] and [r.number for r in doc.refused] == [2] and doc.notes


def test_plan_of_named_prs_skips_the_label_rule():
    out = invoke(runtime_for(listing([pr(1, ["web/a.ts"], labels=[]), pr(2, ["web/b.ts"], labels=[])])), ["--format", "json", "head", "combine", "plan", "1", "2"])
    assert [m.number for m in combine_mod.CombinePlan.model_validate_json(out.stdout).members] == [1, 2]


def test_refusals_name_the_setting_or_the_read():
    out = invoke(runtime_for(listing([])), ["head", "combine", "plan", "--area", "plugin"])
    assert out.exit_code == 2 and "combine_plugins_repo" in out.stdout + out.stderr
    failing = FakeCommands()
    failing.on(["gh", "pr", "list"], Result(1, "", "HTTP 502"))
    out = invoke(runtime_for(failing), ["head", "combine", "status"])
    assert out.exit_code == 1 and "gh pr list failed" in out.stdout + out.stderr
    out = invoke(runtime_for(listing([]), combine_plugins_repo="o/plugins"), ["head", "combine", "plan", "--area", "plugin"])
    assert out.exit_code == 1 and "no ready plugin members" in out.stdout + out.stderr
    out = invoke(runtime_for(listing([])), ["head", "combine", "plan", "--area", "nope"])
    assert out.exit_code == 2
