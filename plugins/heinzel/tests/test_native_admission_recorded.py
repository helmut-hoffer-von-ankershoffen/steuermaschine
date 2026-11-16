"""Recorded 2026-11-12 from real admissions by the former tool (prepare > prepare.out, apply > apply.out; receipts copied
from the receipts directory). Nothing is stripped: the digests below are over these exact bytes."""
import hashlib
import json
from pathlib import Path

import pytest

from steuermaschine_heinzel.native import admission
from steuermaschine_heinzel.native.admission_lib import focused_admit as a
from steuermaschine_heinzel.native.admission_lib import ops

RECORDED = Path(__file__).parent / "fixtures/admission/recorded"
CASES = json.loads((RECORDED / "INDEX.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_canonical_digest_reproduces_recorded_transactions(case):
    path = RECORDED / case["name"] / "prepare.out"
    raw = path.read_bytes()
    doc = json.loads(raw)
    # the former tool printed canonical JSON plus one newline; the digest excludes the newline
    assert a.canonical(doc) + b"\n" == raw
    result = admission.head_hash(path)
    assert result.sha256 == hashlib.sha256(raw.rstrip(b"\n")).hexdigest() == ops.canonical_sha256(doc)
    assert result.render() == [result.sha256]
    if case["receipt"]:
        receipt = json.loads((RECORDED / case["name"] / "receipt.json").read_text())
        assert receipt["transaction_sha256"] == result.sha256


def test_at_least_one_real_receipt_is_checked():
    assert sum(1 for c in CASES if c["receipt"]) >= 6 and len(CASES) >= 10


def test_core_hashing_reproduces_the_recorded_digests():
    from steuermaschine.hashing import canonical_sha256

    for case in CASES:
        raw = (RECORDED / case["name"] / "prepare.out").read_text()
        assert canonical_sha256(raw) == ops.canonical_sha256(json.loads(raw))
        if case["receipt"]:
            assert canonical_sha256(raw) == json.loads((RECORDED / case["name"] / "receipt.json").read_text())["transaction_sha256"]


def test_registrations():
    from steuermaschine_heinzel import native

    reg = {c.path: c for c in native.commands() if hasattr(c, "writes")}
    assert {"head hash": False, "head blockers": False, "head prepare": True, "head apply": True}.items() <= {p: c.writes for p, c in reg.items()}.items()

