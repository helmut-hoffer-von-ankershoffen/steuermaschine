"""The packet review of `sm head admit` binds its digest to the head: `sm review verdict --approve` records nothing
until that exact commit has the green receipt `sm check` writes."""

from __future__ import annotations

from test_pipeline_cov_head_admit import HEAD, clean_overrides, native  # noqa: F401  (the autouse fixture)
from test_pipeline_head_admit_rejected_packet import _setup

from steuermaschine import verdicts
from steuermaschine.testing import invoke
from steuermaschine_heinzel.pipelines import head_admit


def test_the_packet_digest_cannot_be_approved_before_sm_check_was_green_on_the_head(tmp_path):
    rt, ctx, _sel, _builds = _setup(tmp_path)
    store = rt.providers.store
    with native(rt):
        head_admit.preflight(ctx)
        head_admit.packet(ctx)
        digest, brief, _file = head_admit.packet_review(ctx)
    command = f"sm check {tmp_path / 'wt'}"
    assert store.load(verdicts.KIND, digest)["check"] == {"head": HEAD, "command": command}
    assert f"`{command}` writes it" in brief and HEAD[:12] in brief
    verdicts.register(store, digest, "head admit", "builder-1")  # the run that stopped at the digest, as the engine records its author
    no = invoke(rt, ["review", "verdict", digest, "--approve", "--reviewer", "reviewer-1"])
    assert no.exit_code == 1 and f"Run `{command}`" in no.stderr and "no green check receipt" in no.stderr
    assert store.get_receipt("verdicts", digest) is None
    verdicts.record_check(store, HEAD, {"command": "sm check"})
    assert invoke(rt, ["review", "verdict", digest, "--approve", "--reviewer", "reviewer-1"]).exit_code == 0
    assert store.get_receipt("verdicts", digest)["verdict"] == "approve"


def test_no_packet_means_no_digest_and_no_binding(tmp_path, monkeypatch):
    rt, ctx, _sel, _builds = _setup(tmp_path)
    monkeypatch.setattr(head_admit, "manifest_skip", lambda _ctx: "the merge tier needs no manifest")
    assert head_admit.packet_review(ctx) == ("", "", "")
    assert rt.providers.store.keys(verdicts.KIND) == []
