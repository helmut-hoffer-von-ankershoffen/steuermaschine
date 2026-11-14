"""What moved into this directory: the release step runner and what it runs (rejection, candidate policy and
capacity, snapshot, notes audit). Read by `port/move.py` and registered with the edge module for cross-tool loads.
Part of the reviewed code: the review digest of the release write commands covers this file."""

RUNNER = "hz-release"  # the old file name, as a name: a patch row is a table row, not a command line
# Whole files, moved verbatim: old file -> module in this directory.
MOVED: dict[str, str] = {
    "hz-release": "runner",
    "release_notes_audit.py": "notes_audit",
    "release_rejection.py": "rejection",
    "release_candidate_policy.py": "candidate_policy",
    "release_candidate_capacity.py": "candidate_capacity",
    "release_snapshot.py": "snapshot",
}
# Parts of files: the named top-level definitions and everything they refer to. old file -> (module, purpose, root names).
EXTRACTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
}
# Exact replacements beyond the line rules of port/move.py: (source file, old text, new text, why).
PATCHES: list[tuple[str, str, str, str]] = [
    (
        RUNNER,
        '    work = os.environ.get("HZ_RELEASE_DIR", "/private/tmp/heinzel-air-release")\n',
        '    work = edge.release_dir()  # moved: the release checkout comes from the settings of the native command\n',
        'the release checkout is the setting release_dir; a moved function called directly keeps the old variable and default',
    ),
    (
        RUNNER,
        '            f"approved, from {work} resume with `{Path(__file__).resolve().with_name(\'hz-gate\')} git push --quiet "\n',
        '            f"approved, from {work} resume with `{\' \'.join(edge.gate())} git push --quiet "\n',
        'the hint names the native run lock command instead of the path of the old lock script',
    ),
    (
        RUNNER,
        '    gate = str(Path(__file__).resolve().with_name("hz-gate"))\n    env = os.environ | RELEASE_PR_PUSH_ENV\n',
        '    gate = edge.gate()  # moved: the run lock is `sm gate run --`, no script is started\n    env = os.environ | RELEASE_PR_PUSH_ENV\n',
        'the release branch push runs under the native run lock',
    ),
    (
        RUNNER,
        '        pushed = edge.run([gate, "git", "push", "--quiet", "origin", f"{checked_head}:{destination}"],\n',
        '        pushed = edge.run([*gate, "git", "push", "--quiet", "origin", f"{checked_head}:{destination}"],\n',
        'the lock command is several words now, so it is spliced into the argument list',
    ),
    (
        RUNNER,
        '    gate = str(Path(__file__).resolve().with_name("hz-gate"))\n    argv = [gate, "bash", "scripts/release.sh", bump]\n',
        '    gate = edge.gate()  # moved: the run lock is `sm gate run --`, no script is started\n    argv = [*gate, "bash", "scripts/release.sh", bump]\n',
        "the core repository's own release script runs under the native run lock",
    ),
    (
        RUNNER,
        '    done = edge.run([str(Path(__file__).resolve().with_name("hz-ship")), str(pr)], capture_output=True,\n',
        '    done = edge.run(edge.ship(pr), capture_output=True,  # moved: `sm head ship PR`, no script is started\n',
        'tag-on-green-main only: the bump PR is shipped with the native command',
    ),
    (
        RUNNER,
        '\n\nif __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n',
        '\n',
        'no second way in: the module is reached through its native command only (the review gate sees every entry)',
    ),
    (
        'release_candidate_policy.py',
        "APPROVED_SOURCES = {'.github/workflows/ci.yml': '4ba621c12257aca5ea01cb63c6b96b741e911f761df6a68e92fb3c55b79cdc2c', 'scripts/release_candidate.py': '596afe5f2c5aff2fe384d795af0bf9d1872ab53f41e17080bce43b7e389e0b78'}\n",
        'APPROVED_SOURCES = edge.approved_pins()  # moved: the pins are the hashes of the reviewed files in approved_sources/\n',
        'one source of truth for the pins: the reviewed bytes kept in release_lib/approved_sources/; the two literal hashes are what these files hash to',
    ),
]
# Reviewed patches of the old tools that are not applied there yet and move with the code: (patch file relative to the
# old tools directory, its sha256, the old files it changes or creates, its verdict file). The source of these files
# is "old file plus this patch"; `port/move.py verify` checks the sha256 and applies the patch before it compares.
REVIEWED_PATCHES: list[tuple[str, str, tuple[str, ...], str]] = [
    (
        "../research/hz-release-drop-candidate.patch",
        "658d19ea8ac36a0ae7634135d6d26a1b274d94099b6492a0947bb5cfa8717743",
        (RUNNER, "tests/test_release_drop_candidate.py"),
        "research/hz-release-drop-candidate.verdict.md (APPROVE WITH ONE FIX, the fix is in the patch)",
    ),
]
# Moved tests of this area: source test file -> new basename (unique repo-wide).
TESTS: dict[str, str] = {
    "test_acceptance_identity.py": "test_native_release_acceptance_identity.py",
    "test_acceptance_rerun_replacement.py": "test_native_release_acceptance_rerun_replacement.py",
    "test_release_acceptance_marker.py": "test_native_release_acceptance_marker.py",
    "test_release_candidate_capacity.py": "test_native_release_candidate_capacity.py",
    "test_release_candidate_policy.py": "test_native_release_candidate_policy.py",
    "test_release_compose_held.py": "test_native_release_compose_held.py",
    "test_release_handoff.py": "test_native_release_handoff.py",
    "test_release_notes_audit.py": "test_native_release_notes_audit.py",
    "test_release_pause.py": "test_native_release_pause.py",
    "test_release_pr_acceptance_advisory.py": "test_native_release_pr_acceptance_advisory.py",
    "test_release_pr_gate.py": "test_native_release_pr_gate.py",
    "test_release_producer.py": "test_native_release_producer.py",
    "test_release_promote.py": "test_native_release_promote.py",
    "test_release_rejection.py": "test_native_release_rejection.py",
    "test_release_snapshot.py": "test_native_release_snapshot.py",
    "test_tag_on_main.py": "test_native_release_tag_on_main.py",
    "test_release_drop_candidate.py": "test_native_release_drop_candidate.py",
    "test_release_ask_verify_brief.py": "test_native_release_ask_verify_brief.py",
}
# Exact replacements in the moved tests beyond the import rule: (test file, old, new, why).
TEST_PATCHES: list[tuple[str, str, str, str]] = [
    (
        'test_release_candidate_capacity.py',
        "    blobs = {cap.strict.WORKFLOW: (TOOLS / 'tests/fixtures/release-candidate-ci.yml').read_bytes(),\n             cap.strict.RECORDER: (TOOLS / 'release_candidate.py').read_bytes()}\n",
        '    blobs = {cap.strict.WORKFLOW: cap.policy.edge.approved.blob(cap.strict.WORKFLOW),\n             cap.strict.RECORDER: cap.policy.edge.approved.blob(cap.strict.RECORDER)}\n',
        'the reviewed workflow and recorder are the files the pins are computed from (the old fixture was a stale copy: 48 failures)',
    ),
    (
        'test_release_candidate_capacity.py',
        "    assert hashlib.sha256((TOOLS / 'release_candidate.py').read_bytes()).hexdigest() == cap.policy.APPROVED_SOURCES[cap.strict.RECORDER]\n",
        "    assert hashlib.sha256(__import__('moved_support').unmoved(cap.strict)).hexdigest() == cap.policy.APPROVED_SOURCES[cap.strict.RECORDER]\n",
        'the reader this package runs is the reviewed recorder: its module without the edge lines hashes to the pin',
    ),
    (
        'test_release_candidate_capacity.py',
        "    rows = json.loads((TOOLS / 'tests/fixtures/candidate-reused-carriers.json').read_text())\n",
        "    rows = json.loads((Path(__file__).resolve().parent / 'fixtures/candidate-reused-carriers.json').read_text())\n",
        'the fixture is copied byte for byte next to the moved test',
    ),
    (
        'test_release_candidate_policy.py',
        '    assert hashlib.sha256((TOOLS / "release_candidate.py").read_bytes()).hexdigest() == policy.APPROVED_SOURCES[release_candidate.RECORDER]\n    reviewed_workflow = TOOLS / "tests/fixtures/release-candidate-ci.yml"\n',
        '    assert hashlib.sha256(__import__("moved_support").unmoved(release_candidate)).hexdigest() == policy.APPROVED_SOURCES[release_candidate.RECORDER]\n    reviewed_workflow = policy.edge.approved.file(release_candidate.WORKFLOW)\n',
        'the reader this package runs is the reviewed recorder, and the reviewed workflow is the file its pin is computed from',
    ),
    (
        'test_release_candidate_policy.py',
        '    fixture = (TOOLS / "tests/fixtures/release-candidate-ci.yml").read_bytes()\n    recorder = (TOOLS / "release_candidate.py").read_bytes()\n',
        '    fixture = policy.edge.approved.blob(release_candidate.WORKFLOW)\n    recorder = policy.edge.approved.blob(release_candidate.RECORDER)\n',
        'the reviewed bytes come from the one place the pins are computed from (the old fixture was a stale copy: 2 failures)',
    ),
    (
        'test_release_pr_gate.py',
        '        if command[0].endswith("hz-gate") and command[1:4] == ["bash", "scripts/release.sh", "patch"]:\n',
        '        if command == [*release.edge.gate(), "bash", "scripts/release.sh", "patch"]:\n',
        'the lock command is the native one',
    ),
    (
        'test_release_pr_gate.py',
        '    gate = str(Path(release.__file__).resolve().with_name("hz-gate"))\n    script_call = next((command, kwargs) for command, kwargs in calls if "scripts/release.sh" in command)\n    assert script_call == ([gate, "bash", "scripts/release.sh", "patch"], {\n',
        '    gate = release.edge.gate()\n    script_call = next((command, kwargs) for command, kwargs in calls if "scripts/release.sh" in command)\n    assert script_call == ([*gate, "bash", "scripts/release.sh", "patch"], {\n',
        'the lock command is the native one',
    ),
    (
        'test_release_pr_gate.py',
        '    push_call = next((command, kwargs) for command, kwargs in calls if command[:2] == [gate, "git"])\n    assert push_call[0] == [gate, "git", "push", "--quiet", "origin", f"{checked}:refs/heads/release/v0.9.17"]\n',
        '    push_call = next((command, kwargs) for command, kwargs in calls if command[:len(gate) + 1] == [*gate, "git"])\n    assert push_call[0] == [*gate, "git", "push", "--quiet", "origin", f"{checked}:refs/heads/release/v0.9.17"]\n',
        'the lock command is the native one',
    ),
    (
        'test_release_pr_gate.py',
        '    gate = str(Path(release.__file__).resolve().with_name("hz-gate"))\n\n    def run(command, **kwargs):\n        calls.append(command)\n        if command[0] == gate:\n            if missing:\n                raise FileNotFoundError(gate)\n',
        '    gate = release.edge.gate()\n\n    def run(command, **kwargs):\n        calls.append(command)\n        if command[:len(gate)] == gate:\n            if missing:\n                raise FileNotFoundError(gate[0])\n',
        'the lock command is the native one',
    ),
    (
        'test_release_pr_gate.py',
        '    assert sum(command[0] == gate for command in calls) == 1\n    assert not any(command[:2] == [gate, "git"] for command in calls)\n',
        '    assert sum(command[:len(gate)] == gate for command in calls) == 1\n    assert not any(command[:len(gate) + 1] == [*gate, "git"] for command in calls)\n',
        'the lock command is the native one',
    ),
    (
        'test_release_pr_gate.py',
        '    monkeypatch.setenv("HZ_RELEASE_DIR", str(tmp_path / "release-work"))\n    return release\n',
        '    monkeypatch.setenv("HZ_RELEASE_DIR", str(tmp_path / "release-work"))\n    (tmp_path / "release-work/scripts").mkdir(parents=True)\n    (tmp_path / "release-work/scripts/release_notes.py").write_text("")  # the cut reads it to see whether it takes --ref\n    (tmp_path / "release-work/CHANGELOG.md").write_text("## v0.9.17\\n")  # the notes audit of the cut reads it (it runs, unstubbed)\n    return release\n',
        'the fake checkout gets the two files the cut reads directly since the notes audit and the --ref probe were added; the audit itself runs; no assertion changes',
    ),
    (
        'test_release_rejection.py',
        "sys.path.insert(0, str(Path(sys.argv[1]).parent))\nloader = importlib.machinery.SourceFileLoader('isolated_release', sys.argv[1])\nrel = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))\nloader.exec_module(rel)\n",
        'from steuermaschine_heinzel.native.release_lib import runner as rel\n',
        'the child process imports the moved module instead of loading the script by its path',
    ),
]
# Test fixtures copied byte for byte: source under tests/fixtures/ -> the same name under tests/moved/fixtures/.
FIXTURES: tuple[str, ...] = ('acceptance-snapshot.yml', 'candidate-reused-carriers.json', 'pypi-legacy.yml', 'rejection-compose.yml')
# How many lines the rules and patches change in the moved modules, and in the moved tests. `port/move.py verify` fails
# when a count moves, so a new patch cannot slip in unseen: whoever adds one changes the number in the same commit.
EDGE_LINES = 48
TEST_EDGE_LINES = 35
# Lines the rules and patches remove from the moved modules (a patch that only deletes adds no line to the count above).
EDGE_REMOVED_LINES = 47
