"""g3 trust boundary: a packet runs its base's scripts, so the base must be on origin/main (packet_lib/ops.trusted_base),
and the base's scripts are imported as a regular package from the scratch directory (focused_manifest)."""

import json
import subprocess
from pathlib import Path

import pytest
from test_native_packet_manifest import _git, _write, core, generate  # noqa: F401  (core is a fixture)

from steuermaschine_heinzel.native.packet_lib import focused_manifest as fm
from steuermaschine_heinzel.native.packet_lib import ops


@pytest.fixture
def made(request):
    """The manifest tests' Core repository (their `core` fixture), with origin/main following main."""
    return request.getfixturevalue("core")


def build(made, base, out="p.json"):
    return ops.build_packet(made["repo"], "feature", base, made["tmp"] / out, junit=[f"python={made['junit']}"])


def test_a_base_off_main_with_its_own_scripts_is_refused_before_they_run(made, tmp_path):
    repo, marker = made["repo"], tmp_path / "RAN"
    _git(repo, "checkout", "-q", "-b", "evil", made["base"])
    _write(repo, {"scripts/affected.py": f"open({str(marker)!r}, 'w')\n"})
    _git(repo, "commit", "-qam", "a base that is not main")
    evil = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    with pytest.raises(ops.PacketRefused, match="neither origin/main nor an ancestor"):
        build(made, evil)
    assert not marker.exists()


def test_main_and_its_ancestors_are_trusted_and_a_missing_origin_main_refuses(made):
    assert build(made, made["base"]).status == "built"
    _git(made["repo"], "symbolic-ref", "--delete", "refs/remotes/origin/main")
    with pytest.raises(ops.PacketRefused, match="cannot read origin/main"):
        build(made, made["base"])


def test_verify_refuses_a_packet_whose_base_is_off_main(made):
    built = build(made, made["base"])
    packet = json.loads((made["tmp"] / "p.json").read_bytes())
    repo = made["repo"]
    _git(repo, "checkout", "-q", "-b", "side", made["base"])
    _write(repo, {"scripts/related_tests.py": "# changed\n"})
    _git(repo, "commit", "-qam", "side")
    packet["manifest"]["base"] = _git(repo, "rev-parse", "HEAD")
    tampered = made["tmp"] / "tampered.json"
    tampered.write_text(json.dumps(packet))
    assert built.status == "built"
    with pytest.raises(ops.PacketRefused, match="neither origin/main nor an ancestor"):
        ops.verify_packet(repo, tampered, junit=[f"python={made['junit']}"])


def test_a_side_commit_with_main_scripts_is_trusted(made):
    repo = made["repo"]
    _git(repo, "checkout", "-q", "-b", "predicted", made["base"])
    _write(repo, {"docs/x.md": "x\n"})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a predicted squash that leaves scripts/ alone")
    predicted = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "feature")
    assert ops.trusted_base(fm.Repo(repo), predicted) == predicted


def test_the_protected_driver_imports_a_regular_scripts_package(made, monkeypatch):
    repo = fm.Repo(made["repo"])
    _git(made["repo"], "checkout", "-q", "main")
    _write(made["repo"], {"scripts/focused_plan.py": "def protected_blobs(commit):\n    return {}\n"})
    _git(made["repo"], "commit", "-qam", "protected_blobs")
    base = _git(made["repo"], "rev-parse", "HEAD")
    seen = {}
    real = subprocess.run

    def spy(argv, **kw):
        if "-c" in argv and argv[argv.index("-c") + 1] == fm.PROTECTED_DRIVER:
            seen["init"] = (Path(argv[argv.index("-c") + 2]) / "scripts" / "__init__.py").is_file()
        return real(argv, **kw)

    monkeypatch.setattr(fm.subprocess, "run", spy)
    assert fm.base_protected_blobs(repo, base, base) == {}
    assert seen == {"init": True}
