"""Edges of the alarm rules' small helpers: unreadable times, check names and results, and a non-object slot-size answer."""

from __future__ import annotations

from steuermaschine_heinzel.native.alarm_lib import rules


def test_unreadable_times_are_none_and_no_time_is_zero_minutes():
    assert rules.stamp("not a time") is None and rules.stamp("2026-11-21T08:00:00Z") is not None
    assert rules.minutes(None, 100.0) == 0 and rules.minutes(60.0, 180.0) == 2 and rules.minutes(500.0, 100.0) == 0


def test_check_names_fall_back_to_the_context_then_a_question_mark():
    assert rules.check_name({"name": "a"}) == "a" and rules.check_name({"context": "c"}) == "c" and rules.check_name({}) == "?"


def test_commit_statuses_and_unfinished_runs_have_one_word_each():
    assert rules.check_result({"state": "EXPECTED"}) == "PENDING" and rules.check_result({"state": "SUCCESS"}) == "SUCCESS"
    assert rules.check_result({"state": "FAILURE"}) == "FAILURE" and rules.check_result({"state": ""}) == "PENDING"
    assert rules.check_result({"status": "IN_PROGRESS"}) == "PENDING"


def test_a_slot_size_answer_that_is_not_a_table_raises_no_alarm():
    assert rules.slot_fill_alarm(None) is None and rules.slot_fill_alarm("boom") is None
