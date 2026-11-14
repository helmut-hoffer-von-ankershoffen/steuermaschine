"""The combiner: area rules, refusals, clusters, the plan, subjects, meta and the read commands on a fake context."""

import json
import time

import policy_kit as kit
import pytest

from steuermaschine_heinzel.native.combine_lib import combine as c

PINS = tuple(sorted(kit.manifest()["sources"]))


@pytest.fixture(autouse=True)
def pinned_from_policy(monkeypatch):
    """The pinned files are the sources of Core's release policy at main; here the kit's, without a GitHub read."""
    monkeypatch.setattr(c, "PINNED", PINS)
    monkeypatch.setattr(c, "approved_sources", lambda gh, repo: PINS)


def member(n, **kw):
    base = dict(number=n, title=f"fix(web): thing {n}", head=f"fix/t{n}", sha="a" * 40, draft=True,
                labels=["combine:web"], files=[f"web/f{n}.ts"], lines=10, area="web", created=f"2026-01-0{n}T00:00:00Z")
    return c.Member(**(base | kw))


def test_file_and_member_area():
    assert c.is_neutral("docs/x") and c.is_neutral("a.md") and not c.is_neutral("src/a.py")
    assert c.file_area("README.md") == "neutral" and c.file_area(".github/x") == "tools" and c.file_area("Makefile") == "tools"
    assert c.file_area("plugins/clock/a.py", True) == "plugin:clock" and c.file_area("other", True) == "plugin"
    assert c.file_area("web/a") == "web" and c.file_area("src/a") == "system"
    assert c.solo_file("src/auth/login.py") and not c.solo_file("docs/auth.md") and not c.solo_file("src/ok.py")
    assert c.member_area([".github/x", "web/a"]) == "tools"
    assert c.member_area(["plugins/clock/a"], True) == "plugin:clock"
    assert c.member_area(["plugins/clock/a", "plugins/zoom/a"], True) == "plugin"
    assert c.member_area(["README.md"], True) == "neutral" and c.member_area(["README.md"]) == "neutral"
    assert c.member_area(["web/a"]) == "web" and c.member_area(["web/a", "src/b"]) == "system"
    assert c.area_of_label(["x", "combine:web"]) == "web" and c.area_of_label(["combine:nope"]) is None
    assert c.counted("src/a") and not c.counted("uv.lock") and not c.counted("docs/a")


def test_from_gh():
    d = {"number": 3, "title": "t", "headRefName": "h", "headRefOid": "s", "isDraft": True, "labels": [{"name": "combine:system"}, "plain"],
         "mergeable": None, "files": [{"path": "plugins/clock/a.py", "additions": 2, "deletions": 3}],
         "statusCheckRollup": [{"conclusion": "failure"}, {"state": "pending"}], "createdAt": "x", "body": None}
    m = c.Member.from_gh(d)
    assert m.area == "system" and m.lines == 5 and m.mergeable == "UNKNOWN" and m.checks == ["FAILURE", "PENDING"] and m.area_root == "system"
    m = c.Member.from_gh(dict(d, labels=[]), plugins=True)
    assert m.area == "plugin:clock"
    m = c.Member.from_gh(dict(d, labels=[{"name": "combine:plugin"}]), plugins=True)
    assert m.area == "plugin:clock"


def test_refusals():
    ok = member(1)
    assert c.refusal(ok, "web") == ""
    assert "release or combine" in c.refusal(member(1, head="combine/x"), "web")
    assert "not a draft" in c.refusal(member(1, draft=False), "web")
    assert "no combine:<area> label" in c.refusal(member(1, labels=[]), "web")
    assert c.refusal(member(1, labels=[]), "web", explicit=True) == ""
    assert "conflicts" in c.refusal(member(1, mergeable="CONFLICTING"), "web")
    assert "checks failing (FAILURE)" in c.refusal(member(1, checks=["FAILURE"]), "web")
    pinned = c.PINNED[0]
    assert "pinned" in c.refusal(member(1, files=[pinned]), "web")
    assert "solo class" in c.refusal(member(1, files=["src/auth/x.py"]), "web")
    assert "never combined" in c.refusal(member(1), "tools")
    assert c.refusal(member(1, files=[pinned]), "tools").startswith("tools") is True


def test_unknown_pinned_files_refuse_every_member(monkeypatch):
    monkeypatch.setattr(c, "PINNED", None)
    assert "pinned files unknown" in c.refusal(member(1), "web")


def test_pinned_files_are_the_sources_of_the_policy_at_core_main(monkeypatch):
    monkeypatch.undo()
    head = "e" * 40
    answers = {"repos/o/core/commits/main": {"sha": head},
               **{f"repos/o/core/{path}": value for path, value in kit.contents_routes(head, kit.files("new")).items()}}

    def gh(*args):
        assert args[0] == "api"
        return (0, json.dumps(answers[args[1]])) if args[1] in answers else (1, "HTTP 404")

    assert c.approved_sources(gh, "o/core") == PINS
    del answers[f"repos/o/core/contents/{kit.PATH}?ref={head}"]
    assert c.approved_sources(gh, "o/core") is None
    assert c.approved_sources(lambda *a: (0, "not json"), "o/core") is None
    comb = c.Combiner(type("Ctx", (), {"gh": staticmethod(gh), "repo": "o/core"})())
    assert comb.c.repo == "o/core" and c.PINNED is None


def test_topo_and_clusters():
    a, b, d = member(1), member(2, base="fix/t1", files=["web/b.ts"]), member(3, files=["web/f1.ts"])
    order = c.topo_order([b, a])
    assert [m.number for m in order] == [1, 2]
    assert [m.number for m in c.topo_order([member(1, base="fix/t1")])] == [1]
    groups = c.clusters([a, b, d, member(4, files=["web/z.ts"])])
    assert [sorted(m.number for m in g) for g in groups] == [[1, 2, 3], [4]]


def test_plan_selection_and_notes():
    ms = [member(1), member(2, labels=["combine:system"], area="system", files=["src/b.py"]), member(3, draft=False),
          member(4, base="fix/missing"), member(5, base="fix/t1", files=["web/five.ts"]), member(6, area="neutral", labels=["combine:web"]),
          member(7, labels=[], area="web")]
    p = c.plan(ms, "web")
    nums = [m.number for m in p.members]
    assert 1 in nums and 5 in nums and 2 not in nums
    reasons = {m.number: why for m, why in p.refused}
    assert "not a draft" in reasons[3] and "stacked on fix/missing" in reasons[4] and 7 in reasons
    taken = [{"number": 90, "members": [(1, "s")], "files": ["web/five.ts", "web/a", "web/b", "web/c", "web/d"]}]
    p = c.plan(ms, "web", taken)
    assert 1 not in [m.number for m in p.members]
    p = c.plan(ms, "web", taken, explicit=(1, 5))
    assert any("already in combine #90" in why for _, why in p.refused)
    p = c.plan(ms, "web", without=(1,))
    assert 1 not in [m.number for m in p.members]
    p = c.plan([member(1), member(2, files=["web/f1.ts"])], "web", [{"number": 8, "files": ["web/f1.ts"]}])
    assert p.after == 8 and p.notes
    big = [{"number": 8, "files": [f"web/f{i}.ts" for i in range(1, 6)]}]
    p = c.plan([member(i) for i in range(1, 6)], "web", big)
    assert any("fold both" in n for n in p.notes)
    p = c.plan([member(1)], "web")
    assert any("< " in n for n in p.notes)
    assert c.plan([member(1, area="plugin:clock", labels=["combine:plugin"])], "plugin:zoom").members == []


def test_plan_windows_and_clusters_larger_than_window(monkeypatch):
    monkeypatch.setattr(c, "K_MAX", 2)
    many = [member(i, lines=1000, files=[f"web/f{i}.ts"]) for i in range(1, 6)]
    p = c.plan(many, "web", k_max=2)
    assert len(p.members) == 2 and p.deferred and "window full" in p.deferred[0][1]
    shared = [member(i, files=["web/shared.ts"]) for i in range(1, 5)]
    p = c.plan(shared, "web", k_max=2)
    assert len(p.members) == 2 and "larger than one combine" in p.deferred[0][1]
    huge = [member(1, lines=99999)]
    assert len(c.plan(huge, "web").members) == 1


def test_subject_helpers():
    assert c.strip_suffix("fix(a): b (#12) ") == "fix(a): b"
    assert c.shorten("short", 10) == "short"
    assert c.shorten("alpha beta gamma delta", 12) == "alpha beta"
    assert c.shorten("abcdefghijklmnop", 5) == "abcde"
    assert c.member_subject("fix(web): x", 7) == "fix(web): x (#7)"
    long = "fix(web): " + "word " * 30
    out = c.member_subject(long, 7)
    assert out.startswith("fix(web): ") and out.endswith("(#7)") and len(out) <= c.MAX_SUBJECT
    out = c.member_subject("nonconventional " * 10, 5)
    assert len(out) <= c.MAX_SUBJECT


def test_parse_meta_cap_age(monkeypatch):
    assert c.parse_meta("") == {} and c.parse_meta("nothing") == {}
    meta = c.parse_meta("x <!-- hz-combine area=web members=1:aa,2:bb after=7 junk --> y")
    assert meta["members"] == [(1, "aa"), (2, "bb")] and meta["after"] == 7 and meta["area"] == "web"
    assert c.parse_meta("<!-- hz-combine area=web -->")["after"] is None
    assert c.cap_line(0).endswith("ok") and c.cap_line(c.SOFT_CAP).endswith("FULL") and c.cap_line(c.HARD_CAP).endswith("HARD CAP")
    assert c.age_min("garbage", 5) == 0
    now = time.time()
    created = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now - 3600))
    assert 59 <= c.age_min(created, now) <= 61
    assert c.age_min("2999-01-01T00:00:00Z", now) == 0


class Ctx:
    repo = "o/r"

    def __init__(self, prs, plugins=False, rc=0):
        self.prs, self.plugins, self.rc, self.lines = prs, plugins, rc, []

    def gh(self, *args):
        return self.rc, json.dumps(self.prs) if self.rc == 0 else "boom"

    def say(self, text):
        self.lines.append(text)

    def now(self):
        return time.time()


def pr(n, **kw):
    base = {"number": n, "title": f"fix(web): t{n} (#{n})", "headRefName": f"fix/t{n}", "headRefOid": "a" * 40, "isDraft": True,
            "labels": [{"name": "combine:web"}], "files": [{"path": f"web/f{n}.ts", "additions": 1, "deletions": 1}],
            "mergeable": "MERGEABLE", "createdAt": "2020-01-01T00:00:00Z", "body": ""}
    return base | kw


def test_combiner_read_commands():
    combine = pr(50, headRefName="combine/web-1", body="<!-- hz-combine area=web branch=combine/web-1 members=1:aa after=40 -->")
    ctx = Ctx([pr(1), pr(2), pr(3, isDraft=False), pr(4, labels=[], files=[{"path": "web/q.ts"}]), combine])
    comb = c.Combiner(ctx)
    assert comb.status() == 0
    text = "\n".join(ctx.lines)
    assert "#50 combine/web-1 area web, 1 member(s), after #40" in text and "web: 2 ready of 3 draft" in text and "-> combine build --area web" in text
    p, inflight = comb.plan("web")
    comb.print_plan(p, inflight)
    out = "\n".join(ctx.lines)
    assert "+ #2" in out and "not drafts (own CI, ship alone): #3" in out and "- #4" in out and "next: combine build --area web" in out
    with pytest.raises(SystemExit, match="gh pr list failed"):
        c.Combiner(Ctx([], rc=1)).open_prs()


def test_combiner_plugin_areas():
    def clock(n):
        return pr(n, labels=[{"name": "combine:plugin"}], files=[{"path": "plugins/clock/a.py"}])
    ctx = Ctx([clock(1), clock(2), pr(3, labels=[{"name": "combine:plugin"}], files=[{"path": "plugins/zoom/a.py"}])], plugins=True)
    comb = c.Combiner(ctx)
    assert comb.area_for("web", None, []) == "web"
    assert comb.area_for("plugin", "zoom", []) == "plugin:zoom"
    members, _ = comb.split(comb.open_prs())
    assert comb.area_for("plugin", None, members) == "plugin:clock"
    with pytest.raises(SystemExit, match="no ready plugin members"):
        comb.area_for("plugin", None, [])
    p, inflight = comb.plan("plugin")
    assert p.area == "plugin:clock"
    comb.print_plan(p, inflight)
    assert "--plugin clock" in "\n".join(ctx.lines)
    ctx.lines.clear()
    comb.status()
    assert "plugin:clock" in "\n".join(ctx.lines)
