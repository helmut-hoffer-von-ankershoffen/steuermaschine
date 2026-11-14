import json

import pytest
from test_native_packet_manifest import _git, _write, core, generate  # noqa: F401
from test_native_packet_manifest_merge_tier import PLAN, PROTECTED_BLOBS, _focused, tier  # noqa: F401


@pytest.fixture
def merge_tier_core(request):
    """The merge-tier Core repository of the manifest tests (their `tier` fixture)."""
    return request.getfixturevalue("tier")


def test_candidate_cannot_pick_or_run_its_own_protected_set(merge_tier_core, capsys, tmp_path):
    core_repo = merge_tier_core
    repo = core_repo["repo"]
    marker = tmp_path / "PWNED"
    _git(repo, "checkout", "-q", "main")
    _write(repo, {"scripts/focused_plan.py": PLAN + PROTECTED_BLOBS})
    _git(repo, "commit", "-qam", "protected_blobs base")
    core_repo["base"] = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    _git(repo, "merge", "-q", "--no-edit", "main")
    evil = PLAN + f"\ndef protected_blobs(commit):\n    open({str(marker)!r}, 'w').write('x')\n    return {{}}\n"
    _write(repo, {"scripts/focused_plan.py": evil, "scripts/ci_workload.py": f"open({str(marker)!r}, 'w')\n"})
    _git(repo, "commit", "-qam", "candidate rewrites its own plan")
    _focused(core_repo, {"python": ["tests/unit/a_test.py"]})
    code, path = generate(core_repo)
    err = capsys.readouterr().err
    assert code == 0, err
    assert not marker.exists()
    execution = json.loads(path.read_bytes())["manifest"]["execution"]
    assert {"scripts/focused_plan.py", "Makefile", "uv.lock"} <= set(execution)
    assert execution["scripts/focused_plan.py"] == _git(repo, "rev-parse", "feature:scripts/focused_plan.py")
    assert "protected execution changed: scripts/focused_plan.py" in err
