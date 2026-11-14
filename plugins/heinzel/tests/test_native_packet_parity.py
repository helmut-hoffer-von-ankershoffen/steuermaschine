"""Parity with packets the former tools built (fixtures/packet/recorded, copied unchanged 2026-11-12 from the packet
directories). The checks are those of tests/packet_parity.py; they need a local Git repository that still holds the
recorded commits, named by PACKET_PARITY_CORE (no default path), and are skipped without one."""

import json

import packet_parity as parity
import pytest

needs_core = pytest.mark.skipif(not parity.available(), reason="PACKET_PARITY_CORE names no repository that holds the recorded commits")


@needs_core
@pytest.mark.parametrize("label, check", parity.CHECKS, ids=[label for label, _ in parity.CHECKS])
def test_recorded_packets_rebuild_byte_identical(label, check, tmp_path):
    assert check(parity.DEFAULT_CORE, tmp_path) == []


@pytest.mark.parametrize("name", parity.ALL)
def test_the_recorded_packet_is_the_one_its_record_and_review_name(name):
    directory, record = parity.RECORDED / name, parity.record(name)
    packet = json.loads((directory / "packet.json").read_text())
    assert parity.sha(directory / "packet.json") == record["packet_sha256"]
    assert packet["review"]["path"] == record["review"] == f"{name}/REVIEW.md" and packet["review"]["sha256"] == parity.sha(directory / "REVIEW.md")
    assert packet["manifest"]["head"] == record["head"] and packet["manifest"]["base"] == record["main"] and packet["manifest"]["tree"] == record["tree"]
