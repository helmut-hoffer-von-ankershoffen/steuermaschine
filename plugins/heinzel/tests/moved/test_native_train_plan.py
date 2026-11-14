import sys
from datetime import datetime, timezone
from moved_support import TOOLS
sys.path.insert(0, str(TOOLS))
from steuermaschine_heinzel.native.train_lib import plan as p

chk = lambda c, s="COMPLETED", a="2026-11-07T20:00:00Z", b="2026-11-07T20:30:00Z": {"name": "J", "status": s, "conclusion": c, "startedAt": a, "completedAt": b}
NOW = datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc)


def row(**kw):
    return p.plan_row({"number": 1, "state": "OPEN", "mergeStateStatus": "BLOCKED", "statusCheckRollup": [chk("SUCCESS")]} | kw, {}, NOW)


def test_rows():
    assert row()[3] == "green" and row()[4] == 30 and row()[5] == "sm head ship 1"
    assert row(mergeStateStatus="DIRTY")[5].startswith("rebase")
    assert row(state="MERGED")[1] == "MERGED"
    assert row(statusCheckRollup=[chk("FAILURE")])[3].startswith("FAIL") and row(statusCheckRollup=[chk(None, "IN_PROGRESS")])[3] == "pending"
    assert row(isDraft=True)[1] == "DRAFT"
    assert "merged 1/2" in p.table([row(state="MERGED"), row()])[-1]


def test_red_while_running():
    r = row(statusCheckRollup=[chk("FAILURE"), chk(None, "IN_PROGRESS")])
    assert r[3].startswith("pending, RED 1") and "faillog 1 now" in r[5]


def test_queue_wins_label():
    r = p.plan_row({"number": 1, "state": "OPEN", "mergeStateStatus": "CLEAN", "statusCheckRollup": [chk("SUCCESS")]}, {"1": "queued"}, NOW)
    assert r[1] == "QUEUED" and "queue merges" in r[5]


def test_local_time_not_utc():
    now = datetime(2026, 11, 7, 21, 40, tzinfo=timezone.utc)
    t = p.local_to_utc(now.astimezone().strftime("%H:%M"), now)
    assert t == now.replace(second=0, microsecond=0)
    ahead = p.local_to_utc((now.astimezone().replace(second=0) + __import__("datetime").timedelta(minutes=30)).strftime("%H:%M"), now)
    assert ahead < now
    out = p.merged_lines([{"number": 5, "title": "x", "mergedAt": "2026-11-07T21:26:56Z"}, {"number": 4, "title": "old", "mergedAt": "2026-11-07T20:00:00Z"}], datetime(2026, 11, 7, 21, 0, tzinfo=timezone.utc))
    assert out[0].startswith("1 merged since") and "#5" in out[1] and "local /" in out[1]
