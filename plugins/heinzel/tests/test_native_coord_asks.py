"""`sm doc ask`, `sm doc asks`, `sm doc asks-ack`. The assertions of the former ask and audit tests moved here; the
transcript fixture is the old test's (synthetic records in the transcript's shape)."""

import json
import os

import pytest
from coord_kit import NOW, app_runtime, home, native_runtime  # noqa: F401  (fixtures)

from steuermaschine import journal as core_journal
from steuermaschine.native import using
from steuermaschine.testing import invoke
from steuermaschine_heinzel.native.asks_lib import asks, legacy

TABLE = "### Now, in this order\n\n| Item | Status | Next step | Where |\n|---|---|---|---|\n| old | doing | x | |\n\n### Next\n"


def rec(kind, ts, **extra):
    return {"type": kind, "timestamp": ts, **extra}


def human(ts, content, uuid):
    return rec("user", ts, uuid=uuid, origin={"kind": "human"}, message={"role": "user", "content": content})


def queued(ts, prompt, source):
    return rec("attachment", ts, uuid="a" + source, attachment={"type": "queued_command", "prompt": prompt, "source_uuid": source, "origin": {"kind": "human"}})


@pytest.fixture
def transcript(tmp_path):
    lines = [
        human("2026-01-01T10:00:00.000Z", "first ask: build the thing", "u1"),
        rec("user", "2026-01-01T10:01:00.000Z", uuid="u2", message={"role": "user", "content": [{"type": "tool_result", "content": "result"}]}),
        rec("user", "2026-01-01T10:02:00.000Z", uuid="u3", origin={"kind": "task-notification"}, message={"role": "user", "content": "<task-notification>x</task-notification>"}),
        rec("user", "2026-01-01T10:03:00.000Z", uuid="u4", isMeta=True, message={"role": "user", "content": "Hourly status for the user"}),
        rec("user", "2026-01-01T10:04:00.000Z", uuid="u5", isCompactSummary=True, origin={"kind": "human"}, message={"role": "user", "content": "This session is being continued"}),
        rec("user", "2026-01-01T10:05:00.000Z", uuid="u6", isSidechain=True, origin={"kind": "human"}, message={"role": "user", "content": "subagent prompt"}),
        queued("2026-01-01T10:06:00.000Z", "mid-turn ask\nwith newline", "s1"),
        rec("attachment", "2026-01-01T10:07:00.000Z", attachment={"type": "queued_command", "prompt": "<task-notification>y", "origin": {"kind": "task-notification"}}),
        human("2026-01-01T10:08:00.000Z", [{"type": "text", "text": "[Image: source: /x.png]"}, {"type": "image", "source": {}}, "not a part"], "u7"),
        queued("2026-01-01T10:08:30.000Z", "mid-turn ask\nwith newline", "s1"),
        human("2026-01-01T10:09:00.000Z", "x" * 400, "u8"),
        human("2026-01-01T10:09:30.000Z", 7, "u10"),
        rec("assistant", "2026-01-01T10:09:40.000Z"),
        ["not", "a", "record"],
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    return path


# ----------------------------------------------------------------------------- ask rows
def test_ask_adds_a_row_and_check_finds_it_until_a_pr_is_named(tmp_path):
    doc = tmp_path / "AGENTS.md"
    doc.write_text(TABLE)
    with using(native_runtime(tmp_path)):
        added = asks.add("Make the logo bigger")
    text = doc.read_text()
    assert text.index("Make the logo bigger") < text.index("### Next") and "| old |" in text
    assert added.row in text.splitlines() and added.render() == [f"asked {added.at[-5:]}: Make the logo bigger"]
    assert asks.open_asks(text, 0, NOW)[0][1] == "Make the logo bigger"
    assert asks.open_asks(text, 1, NOW) == []
    assert len(asks.open_asks(text, 1, NOW + 7200)) == 1
    named = text.replace("within the hour; small text or UI goes through the small-change lane | |", "#407 | |")
    assert asks.open_asks(named, 0, NOW) == []


def test_the_row_squeezes_spaces_and_replaces_the_cell_separator(tmp_path):
    (tmp_path / "AGENTS.md").write_text(TABLE)
    with using(native_runtime(tmp_path, asks_lock_file=str(tmp_path / "asks.lock"))):
        row = asks.add("a  |  b\nc").row
    assert (tmp_path / "asks.lock").exists()
    assert row.startswith("| a / b c | ask ") and row.endswith(f"| {asks.NEXT_STEP} | |")
    assert core_journal.table_end(["x"], "### Now") is None


def test_doc_ask_command_text_json_and_refusals(tmp_path):
    (tmp_path / "AGENTS.md").write_text(TABLE)
    done = invoke(app_runtime(tmp_path), ["doc", "ask", "Make", "the logo bigger"])
    assert done.exit_code == 0 and done.stdout.startswith("asked ") and done.stdout.endswith(": Make the logo bigger\n")
    done = invoke(app_runtime(tmp_path), ["--format", "json", "doc", "ask", "second"])
    assert asks.AskAdded.model_validate(json.loads(done.stdout)).text == "second"
    assert (tmp_path / "AGENTS.md").read_text().count("| ask ") == 2
    assert invoke(app_runtime(tmp_path), ["doc", "ask", "  "]).exit_code == 2
    (tmp_path / "AGENTS.md").write_text("## no table here\n")
    done = invoke(app_runtime(tmp_path), ["doc", "ask", "x"])
    assert done.exit_code == 1 and "no asks table" in done.stderr and (tmp_path / "AGENTS.md").read_text() == "## no table here\n"


# ----------------------------------------------------------------------------- the audit
def test_extracts_only_real_owner_messages(transcript):
    texts = [t for _, t in asks.owner_messages(transcript)]
    assert texts == ["first ask: build the thing", "mid-turn ask with newline", "[image]", "x" * 400]


def test_audit_output_ack_and_exit_codes(tmp_path, transcript):
    rt = app_runtime(tmp_path)
    done = invoke(rt, ["doc", "asks", "--transcript", str(transcript)])
    assert done.exit_code == 1
    out = [line for line in done.stdout.splitlines() if not line.startswith("    ")]
    assert out[0].startswith("MSG ") and "first ask: build the thing" in out[0]
    assert out[3].endswith("...") and len(out[3]) < 180
    assert "4 unreviewed" in out[4] and out[4].endswith("sm doc asks-ack 2026-01-01T10:09:00.000Z")
    assert "    sm beads tasks add 'first ask: build the thing' --ask" in done.stdout.splitlines()
    acked = invoke(rt, ["doc", "asks-ack", "2026-01-01T10:09:00.000Z"])
    assert acked.exit_code == 0 and acked.stdout.startswith("acknowledged through 01-01 ")
    done = invoke(rt, ["doc", "asks", "--transcript", str(transcript)])
    assert done.exit_code == 0 and done.stdout.splitlines() == ["no unreviewed owner messages", "no open asks older than 1 h"]


def test_message_arriving_after_audit_is_not_acked(tmp_path, transcript):
    rt = app_runtime(tmp_path)
    shown = json.loads(invoke(rt, ["--format", "json", "doc", "asks", "--transcript", str(transcript)]).stdout)
    with transcript.open("a") as handle:
        handle.write(json.dumps(human("2026-01-01T11:00:00.000Z", "late ask", "u9")) + "\n")
    invoke(rt, ["doc", "asks-ack", shown["through"]])
    done = invoke(rt, ["doc", "asks", "--transcript", str(transcript)])
    assert done.exit_code == 1 and "late ask" in done.stdout and "first ask" not in done.stdout


def test_default_transcript_is_newest_top_level_file(tmp_path):
    project = tmp_path / "transcripts"
    (project / "sub/subagents").mkdir(parents=True)
    old, new = project / "old.jsonl", project / "new.jsonl"
    old.write_text(json.dumps(human("2026-01-01T09:00:00.000Z", "old", "o")) + "\n")
    new.write_text(json.dumps(human("2026-01-01T09:30:00.000Z", "new", "n")) + "\n")
    (project / "sub/subagents/deep.jsonl").write_text("{}\n")
    os.utime(old, (1, 1))
    assert asks.newest_transcript(project) == new
    with using(native_runtime(tmp_path, asks_transcripts_dir=str(project))):
        assert [m.text for m in asks.audit().messages] == ["new"]


def test_no_transcript_is_a_refusal_that_names_the_option(tmp_path):
    done = invoke(app_runtime(tmp_path), ["doc", "asks"])
    assert done.exit_code == 1 and "no session transcript found" in done.stderr and "--transcript FILE" in done.stderr


def test_asks_lists_old_ask_rows_without_a_pull_request(tmp_path, transcript):
    (tmp_path / "AGENTS.md").write_text("| logo | ask 2020-01-01 00:00 | next | |\n| done | ask 2020-01-01 00:00 | #407 | |\n")
    rt = app_runtime(tmp_path)
    invoke(rt, ["doc", "asks-ack", "2026-01-01T10:09:00.000Z"])
    done = invoke(rt, ["doc", "asks", "--transcript", str(transcript), "--hours", "2"])
    assert done.exit_code == 1 and done.stdout.splitlines()[0] == "no unreviewed owner messages"
    assert done.stdout.splitlines()[1].startswith("ASK ") and done.stdout.splitlines()[1].endswith(" h with no PR: logo")
    doc = asks.Asks.model_validate(json.loads(invoke(rt, ["--format", "json", "doc", "asks", "--transcript", str(transcript)]).stdout))
    assert [a.text for a in doc.open] == ["logo"] and doc.acknowledged_through == "2026-01-01T10:09:00.000Z"


def test_the_former_watermark_is_adopted_read_only_and_never_moves_backwards(tmp_path, transcript):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "ask-watermark.json").write_text(json.dumps({"ts": "2026-01-01T10:06:30.000Z", "pending": "x"}))
    before = (cache / "ask-watermark.json").read_text()
    rt = native_runtime(tmp_path, legacy_state_dir=str(cache))
    with using(rt):
        assert [m.text for m in asks.audit(transcript).messages] == ["[image]", "x" * 400]
        back = asks.ack("2026-01-01T10:00:00.000Z")
        assert not back.moved and back.through == "2026-01-01T10:06:30.000Z" and "(unchanged)" in back.render()[0]
        assert asks.ack("2026-01-01T10:08:00.000Z").moved
        assert [m.text for m in asks.audit(transcript).messages] == ["x" * 400]
    assert (cache / "ask-watermark.json").read_text() == before
    (cache / "ask-watermark.json").write_text("[]")
    assert legacy.watermark(cache) == "" and legacy.watermark(tmp_path / "none") == ""


def test_ack_refuses_what_is_not_a_timestamp(tmp_path):
    done = invoke(app_runtime(tmp_path), ["doc", "asks-ack", "yesterday"])
    assert done.exit_code == 2 and "is not a timestamp" in done.stderr
    assert asks.AsksAcked(through="", moved=False).render() == ["acknowledged through nothing (unchanged)"]


def test_titles_and_short_times():
    assert asks.title("Make the logo bigger. And the footer smaller.") == "Make the logo bigger"
    assert asks.title("x" * 100) == "x" * 71 + "…"
    assert asks.short_time("not a time at all, really") == "not a time at al"
    assert asks.owner_text(None) == ""


# ----------------------------------------------------------------------------- review findings B1, B2
BROKEN = "caf\udce9 cut mid-character"  # what an argument with one non-UTF-8 byte looks like in Python


def test_an_argument_that_cannot_be_encoded_leaves_the_document_byte_identical(tmp_path):
    doc = tmp_path / "AGENTS.md"
    doc.write_text(TABLE)
    before = doc.read_bytes()
    done = invoke(app_runtime(tmp_path), ["doc", "ask", BROKEN])
    assert done.exit_code != 0 and doc.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".AGENTS.md")] == []  # no temporary file is left behind


def test_the_rewrite_is_atomic_keeps_the_mode_and_other_bytes(tmp_path):
    doc = tmp_path / "AGENTS.md"
    doc.write_bytes(TABLE.encode() + "Umlaute: äöü ✓\n".encode())
    doc.chmod(0o640)
    inode = doc.stat().st_ino
    with using(native_runtime(tmp_path)):
        row = asks.add("Größer").row
    assert doc.read_bytes() == TABLE.encode().replace(b"| old | doing | x | |\n", b"| old | doing | x | |\n" + row.encode() + b"\n") + "Umlaute: äöü ✓\n".encode()
    assert doc.stat().st_mode & 0o777 == 0o640 and doc.stat().st_ino != inode  # a new file was renamed over the old one


def test_a_heading_without_a_table_below_it_is_refused_and_nothing_is_written(tmp_path):
    doc = tmp_path / "AGENTS.md"
    for text in ("### Now\n\nno table here\n\n## 5. State\n\n| other | table |\n|---|---|\n\n## 8. Log\n- 11-12 22:09 line\n", "### Now\n\njust prose\n", "### Now\n"):
        doc.write_text(text)
        done = invoke(app_runtime(tmp_path), ["doc", "ask", "Make the logo bigger"])
        assert done.exit_code == 1 and "### Now" in done.stderr and doc.read_text() == text, text
    assert core_journal.table_end(["### Now", "", "| a |", "|---|", "## Next", "| b |"], "### Now") == 4
    assert core_journal.table_end(["### Now", "text", "### Sub", "| a |"], "### Now") is None
