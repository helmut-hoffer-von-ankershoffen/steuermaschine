"""What moved into this directory: the old tool file and its module here. Read by `port/move.py` (which writes and
verifies the moved sources) and by `edge.py` (which answers the moved code's own cross-tool loads). Part of the
reviewed code: the review digest of the queue commands covers this file."""

# Whole files, moved verbatim: old file -> module in this directory.
MOVED: dict[str, str] = {
    "hzlib.py": "hzlib",
    "hz-queue": "queue_daemon",
    "hz_combine_notes.py": "combine_notes",
    "known_main_infra.py": "known_main_infra",
    "main_batch.py": "main_batch",
    "main_checkpoint.py": "main_checkpoint",
    "main_checkpoint_reader.py": "main_checkpoint_reader",
    "release_flow.py": "release_flow",
    "release_candidate.py": "release_candidate",
    "hz-merge-train": "merge_train",
    "hz-merge-gate": "merge_gate",
    "hz-diff-guard": "diff_guard",
}
# Moved files that are also started as a program of their own (`python -I FILE`, no package around them): no line of
# them may be changed, or they stop at their first import. old file -> who starts it.
STANDALONE: dict[str, str] = {
    "main_checkpoint_reader.py": "main_checkpoint.isolated_reader runs it with `uv run --no-project ... python -I`, isolated from this package",
}
# Parts of files other slices own: the named top-level definitions and everything they refer to, verbatim, in source
# order, under the source's own imports. old file -> (module, one-line purpose, root names).
EXTRACTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "hz_alarm.py": (
        "stall",
        "Why a queued PR does not merge (stall diagnosis) and the alarm state the queue raises and clears.",
        ("REQUIRED", "green_since", "wait_reason", "stall_alarm", "pending_reason", "process", "clear"),
    ),
    "hz_digest.py": (
        "known_failures",
        "The registry of KNOWN failures: which failing job or test is known, to which bead, until when.",
        ("load_known", "known_bead", "guard_cleared"),
    ),
    "hz-flakes": (
        "flakes_read",
        "Failing jobs and tests of one attempt of a run, read from its log once and cached.",
        ("read_attempt",),
    ),
    "hz-stamp": (
        "bead_stamp",
        "The merge stamp on a bead: a note line and a label, written with the bead tracker's own command.",
        ("do_stamp",),
    ),
}
# Source names of the five files whose `__main__` block is removed (named once here, so the tuples below are data).
DIFF_GUARD = "hz-diff-guard"
MERGE_GATE = "hz-merge-gate"
MERGE_TRAIN = "hz-merge-train"
QUEUE_TOOL = "hz-queue"
MAIN_BATCH = "main_batch.py"
# Exact replacements beyond the line rules of port/move.py: (source file, old text, new text, why).
PATCHES: list[tuple[str, str, str, str]] = [
    (
        DIFF_GUARD,
        '\n\nif __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n',
        "\n",
        "no second way in: the module is reached through its native commands only (the review gate sees every entry; Fable review 2026-11-13, F8)",
    ),
    (
        MAIN_BATCH,
        '\n\nif __name__ == "__main__":\n    main()\n',
        "\n",
        "no second way in: the module is reached through its native commands only (the review gate sees every entry; Fable review 2026-11-13, F8)",
    ),
    (
        MERGE_GATE,
        '\n\nif __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n',
        "\n",
        "no second way in: the module is reached through its native commands only (the review gate sees every entry; Fable review 2026-11-13, F8)",
    ),
    (
        MERGE_TRAIN,
        '\n\nif __name__ == "__main__":\n    main()\n',
        "\n",
        "no second way in: the module is reached through its native commands only (the review gate sees every entry; Fable review 2026-11-13, F8)",
    ),
    (
        QUEUE_TOOL,
        '\n\nif __name__ == "__main__":\n    sys.exit(main(sys.argv[1:]))\n',
        "\n",
        "no second way in: the module is reached through its native commands only (the review gate sees every entry; Fable review 2026-11-13, F8)",
    ),
    (
        "hzlib.py",
        '    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), str(TOOLS / name))\n'
        "    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))\n"
        "    loader.exec_module(module)\n"
        "    return module\n",
        "    return edge.moved(name)  # moved: a fresh copy of the module the tool moved to; never a script\n",
        "a cross-tool load runs the moved module; a tool that is not part of this package raises, nothing is executed from a directory",
    ),
    (
        "hzlib.py",
        '    edge.run([str(TOOLS / "hz-note"), text], capture_output=True, check=False)\n',
        "    edge.note(text)  # moved: the same line in the same document, written here\n",
        "the note script is not started; its one append (lock, date, line) is edge.note",
    ),
    (
        "hzlib.py",
        'str(TOOLS.parent / "research" / "known-failures.toml")',
        'str(edge.records_dir() / "research" / "known-failures.toml")',
        "the known-failures registry is read from the configured records checkout",
    ),
    (
        "hz-queue",
        '    here = Path(__file__).resolve().parent\n    return max((here / name).stat().st_mtime for name in ("hz-queue", "hzlib.py", "hz-merge-train", "hz-merge-gate", "main_batch.py", "known_main_infra.py", "main_checkpoint.py", "main_checkpoint_reader.py", "release_candidate.py", "release_candidate_policy.py"))\n',
        "    return edge.code_changed_at(__file__)  # moved: the loop restarts itself when any file next to this module changed\n",
        "the running loop re-executes itself when its own code changed; its code is now this package, not ten script files",
    ),
    (
        "hz-queue",
        '        edge.run([str(Path(__file__).resolve().parent / "hz-stamp"), item["bead"], "--merged", item["pr"]], capture_output=True, check=False)\n',
        '        edge.stamp_merged(item["bead"], item["pr"])  # moved: the stamp is written here, no script is started\n',
        "the stamp script is not started; its merge stamp is the moved function bead_stamp.do_stamp, output captured and failure ignored as before",
    ),
    (
        "hz-stamp",
        'ROOT = os.path.expanduser("~/Code/heinzel-management")\n',
        "ROOT = str(edge.records_dir())  # moved: the checkout with the bead database, from the plugin's settings\n",
        "the bead tracker runs in the configured records checkout",
    ),
]
# How many lines the rules and patches change in the moved modules, and in the moved tests. `port/move.py verify` fails
# when the count moves, so a new patch cannot slip in unseen: whoever adds one changes this number in the same commit.
EDGE_LINES = 51
TEST_EDGE_LINES = 15
