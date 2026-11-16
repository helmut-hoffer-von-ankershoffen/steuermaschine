"""The public read-only candidate proof refuses any changed reviewed identity."""
import pytest

from steuermaschine.engine import Refuse
from steuermaschine_heinzel.native import release_read
from steuermaschine_heinzel.native.release_lib import candidate_policy

HEAD, BASE, TREE = (c * 40 for c in 'abc')


def test_candidate_proof_reuses_full_verifier_and_requires_exact_review(monkeypatch):
    calls = []
    def verify(number):
        calls.append(number)
        return {'head': HEAD, 'base': BASE, 'tree': TREE, 'run_id': 7, 'run_attempt': 1, 'job_count': 61}
    monkeypatch.setattr(candidate_policy, 'verify_pr', verify)
    proof = release_read.release_candidate_proof(123, HEAD, BASE, TREE)
    assert proof.run_id == 7
    assert proof.render() == ["verified original full candidate run 7 for PR #123",
                              f"head {HEAD} base {BASE} tree {TREE}; 61 jobs"]
    assert calls == [123]
    for head, base, tree in ((BASE, BASE, TREE), (HEAD, TREE, TREE), (HEAD, BASE, BASE)):
        with pytest.raises(Refuse, match='differs'):
            release_read.release_candidate_proof(123, head, base, tree)


def test_candidate_proof_refuses_malformed_and_failed_evidence(monkeypatch):
    with pytest.raises(Refuse, match='exact Git identities'):
        release_read.release_candidate_proof(123, 'short', BASE, TREE)
    def failed(_):
        raise ValueError('incomplete source artifact')
    monkeypatch.setattr(candidate_policy, 'verify_pr', failed)
    with pytest.raises(Refuse, match='candidate proof refused'):
        release_read.release_candidate_proof(123, HEAD, BASE, TREE)
