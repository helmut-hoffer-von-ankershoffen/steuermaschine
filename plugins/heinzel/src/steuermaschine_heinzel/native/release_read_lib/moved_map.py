"""What moved into this directory: the code of the read-only release commands (status screen, chores, published
surfaces, asset probes). Read by `port/move.py` and registered with the edge module for cross-tool loads."""

VERIFY = "hz-verify"  # the old file name, as a name: a patch row is a table row, not a command line
# Whole files, moved verbatim: old file -> module in this directory.
MOVED: dict[str, str] = {
    "hz_chores.py": "chores",
    "hz-verify": "verify",
    "hz_assets.py": "assets",
}
# Parts of files: the named top-level definitions and everything they refer to. old file -> (module, purpose, root names).
EXTRACTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "hz_release_status.py": (
        "status",
        "One-screen release status: the pure parts (which train is active, check counts, summary, text).",
        ('active_train', 'counts', 'summarize', 'render'),
    ),
}
# Exact replacements beyond the line rules of port/move.py: (source file, old text, new text, why).
PATCHES: list[tuple[str, str, str, str]] = [
    (
        'hz_chores.py',
        'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n',
        '',
        'siblings are imported relatively; no directory is put on the import path',
    ),
    (
        'hz_chores.py',
        'CORE_DIR = os.path.expanduser("~/Code/heinzel")\n',
        "CORE_DIR = edge.core_checkout()  # moved: the read-only core checkout, from the plugin's settings\n",
        'the core checkout that pin-diagnose reads is the setting release_core_checkout',
    ),
    (
        'hz_chores.py',
        '\n\nif __name__ == "__main__":\n    main()\n',
        '\n',
        'no second way in: the module is reached through its native command only (the review gate sees every entry)',
    ),
    (
        VERIFY,
        '\n\nif __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n',
        '\n',
        'no second way in: the module is reached through its native command only (the review gate sees every entry)',
    ),
    (
        'hz_assets.py',
        'CORE = os.path.expanduser("~/Code/heinzel")\n',
        "CORE = edge.core_checkout()  # moved: the read-only core checkout, from the plugin's settings\n",
        'the core checkout whose pinned asset list is probed is the setting release_core_checkout',
    ),
    (
        'hz_assets.py',
        '\n\nif __name__ == "__main__":\n    sys.exit(main())\n',
        '\n',
        'no second way in: the module is reached through its native command only (the review gate sees every entry)',
    ),
]
# Moved tests of this area: source test file -> new basename (unique repo-wide).
TESTS: dict[str, str] = {
    "test_assets.py": "test_native_release_assets.py",
    "test_chores.py": "test_native_release_chores.py",
    "hz_release_status_test.py": "test_native_release_status_summary.py",
}
# Exact replacements in the moved tests beyond the import rule: (test file, old, new, why).
TEST_PATCHES: list[tuple[str, str, str, str]] = [
]
# Test fixtures copied byte for byte: source under tests/fixtures/ -> the same name under tests/moved/fixtures/.
FIXTURES: tuple[str, ...] = ()
# How many lines the rules and patches change in the moved modules, and in the moved tests. `port/move.py verify` fails
# when a count moves, so a new patch cannot slip in unseen: whoever adds one changes the number in the same commit.
EDGE_LINES = 10
TEST_EDGE_LINES = 3
# Lines the rules and patches remove from the moved modules (a patch that only deletes adds no line to the count above).
EDGE_REMOVED_LINES = 18
