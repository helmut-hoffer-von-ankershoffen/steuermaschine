"""What moved into this directory: the old tool file and its module here (see queue_lib/moved_map.py for the format).
Part of the reviewed code of the `head` commands of native/ship.py."""

MOVED: dict[str, str] = {
    "hz_push_gate.py": "push_gate_rules",
    "hz-push-gate": "push_gate_cli",
}
EXTRACTS: dict[str, tuple[str, str, tuple[str, ...]]] = {}
PATCHES: list[tuple[str, str, str, str]] = [
    (
        "hz-push-gate",
        "\nsys.exit(main() or OK)\n",
        "\n# moved: the script's last line `sys.exit(main() or OK)` is gone; the gate runs only when called (push_gate.check)\n",
        "the script ran at import; as a module it runs only when called, and has no entry of its own",
    ),
]
# Moved tests of this area, and three of the queue's that moved after the queue's review unit was frozen (its table is
# part of that unit): old test file -> new basename.
TESTS: dict[str, str] = {
    "test_push_gate.py": "test_native_ship_push_gate.py",
    "test_combine_notes.py": "test_native_queue_combine_notes.py",
    "test_diff_guard_flake_groups_brief_gate.py": "test_native_queue_guard_and_neighbours.py",
    "test_hz_watch_queue.py": "test_native_queue_watch_queue.py",
}
# Exact replacements in those tests beyond the import rule: (test file, old, new, why).
TEST_PATCHES: list[tuple[str, str, str, str]] = [
    (
        "test_hz_watch_queue.py",
        "    setup(monkeypatch, lib, queue, [reviewed, current])\n    assert queue.main(REVIEWED_ADD) == 0\n",
        "    setup(monkeypatch, lib, queue, [reviewed, reviewed, current])  # moved: add reads the PR twice (title and body, then the head)\n    assert queue.main(REVIEWED_ADD) == 0\n",
        "stale in the source: since `add` checks the combine notes it reads the PR once more, so the scripted replies were one short; "
        "the test failed alone and passed only in the full run by accident of order",
    ),
    (
        "test_push_gate.py",
        """    path = str(pathlib.Path(__file__).resolve().parent.parent / "hz-push-gate")
    src = open(path).read().replace("sys.exit(main() or OK)", "")
    ns = {"__name__": "hzpg", "__file__": path}
    exec(compile(src, path, "exec"), ns)
""",
        """    from moved_support import load
    ns = vars(load("hz-push-gate"))  # moved: the gate is a module; importing it no longer runs it
""",
        "the test executed the script's text after cutting its last line",
    ),
]
EDGE_LINES = 7
TEST_EDGE_LINES = 9  # also the three queue test files moved after the queue unit was frozen (guard, combine notes, watch)
