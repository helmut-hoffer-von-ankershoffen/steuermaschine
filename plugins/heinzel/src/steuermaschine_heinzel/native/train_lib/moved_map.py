"""What moved into this directory: the release train (the dispatcher tick, one release run, status, clear), the N+1
batch view at the cut (next) and the candidate table (plan) with the digest definitions it needs. Read by
`port/move.py` and registered with the edge module. Part of the reviewed code of the train's write commands."""

TRAIN = "hz-train"  # the old file name, as a name: a patch row is a table row, not a command line
NEXT = "hz_next.py"
PLAN = "hz_plan.py"
DIGEST = "./hz_digest.py"  # the same old file as queue_lib's extract, under another key: this area needs other definitions of it
# Whole files, moved verbatim: old file -> module in this directory.
MOVED: dict[str, str] = {
    "hz-train": "train",
    "hz_next.py": "batch_next",
    "hz_plan.py": "plan",
}
# Parts of files other slices own: old file -> (module, one-line purpose, root names).
EXTRACTS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    DIGEST: (
        "digest",
        "How the candidate table classifies a PR's checks, and the registry of KNOWN failures it names beads from.",
        ("REAL_BAD", "is_focused", "classify_pr", "load_known", "known_bead"),
    ),
}
# Replacements made BEFORE the line rules of port/move.py (a line the rules would refuse): (source, old, new, why).
PRE_PATCHES: list[tuple[str, str, str, str]] = []
# Exact replacements beyond the line rules of port/move.py: (source file, old text, new text, why).
PATCHES: list[tuple[str, str, str, str]] = [
    (
        TRAIN,
        '''        w.note(f"TODO: train v{st['version']} blocked: {reason} (agents fix; then hz-train clear)")
''',
        '''        w.note(f"TODO: train v{st['version']} blocked: {reason} (agents fix; then hz-train clear {st['version']})")  # moved: clear takes the version
''',
        "clear needs the exact version (it never picks one), so the block note names it",
    ),
    (
        TRAIN,
        '''        subprocess.Popen([str(Path(__file__).resolve()), "run", version], stdin=subprocess.DEVNULL,
                         start_new_session=True, env=os.environ)
''',
        '''        subprocess.Popen(edge.train_run(version), stdin=subprocess.DEVNULL,  # moved: `sm daemon train --run X`, no script
                         start_new_session=True, env=os.environ)
''',
        "the detached runner of one release is the native command, started in its own session as before",
    ),
    (
        TRAIN,
        '''        done = edge.run([str(Path(__file__).resolve().with_name("hz-ci-defer")), "--cancel", "--force-release"], capture_output=True, text=True, check=False)
''',
        '''        done = edge.run([*edge.sm(), "ci", "defer", "--cancel", "--force-release"], capture_output=True, text=True, check=False)  # moved: no script
''',
        "freeing runner slots is `sm ci defer --cancel --force-release`",
    ),
    (
        TRAIN,
        '''        return f"hz-ci-defer --cancel --force-release exit {done.returncode}: {sum(l.startswith('cancelled ') for l in lines)} PR run(s) cancelled and recorded"
''',
        '''        return f"sm ci defer --cancel --force-release exit {done.returncode}: {sum(l.startswith('cancelled ') for l in lines)} PR run(s) cancelled and recorded"
''',
        "the line that is noted and kept in the train state names the command that ran",
    ),
    (
        TRAIN,
        '''        shown = edge.run([str(Path(__file__).resolve().with_name("hz-gate")), "uv", "run", "--frozen",
''',
        '''        shown = edge.run([*edge.gate(), "uv", "run", "--frozen",  # moved: `sm gate run --priority 2 --hold-exempt --`
''',
        "the version probe in the release checkout runs under the native run lock, with the priority and the hold exemption as its options",
    ),
    (
        TRAIN,
        '''        return os.environ | {"HZ_GATE_PRIORITY": "2", "HZ_GATE_HOLD_EXEMPT": "1", "HZ_RELEASE_DIR": str(CHECKOUT)}
''',
        '''        return os.environ | {"HZ_RELEASE_DIR": str(CHECKOUT), "SM_HEINZEL__RELEASE_DIR": str(CHECKOUT)}  # moved: the run lock reads no environment
''',
        ("the native step runner takes its checkout from the setting release_dir; the train passes its own checkout that way (and the old "
         "variable with the same value). The run lock takes priority and hold exemption as options of `sm gate run`, never from the "
         "environment, so the two variables are no longer passed"),
    ),
    (
        TRAIN,
        '''        cmd = [str(Path(__file__).resolve().with_name("hz-release")), version, "--intro", intro, "--snapshot-acceptance", "--candidate-gate"]
''',
        '''        cmd = [*edge.sm(), "release", "resume", version, "--intro", intro, "--snapshot-acceptance", "--candidate-gate"]  # moved: no script
''',
        "one release is `sm release resume X --intro ... --snapshot-acceptance --candidate-gate`, as a child process whose lines are read",
    ),
    (
        TRAIN,
        '''        verifier = Path(__file__).resolve().with_name("hz-verify-mini")
        try:
            done = edge.run([str(verifier), "--minimum", version], capture_output=True, text=True,
''',
        '''        verifier = [*edge.sm(), "release", "verify", "--mini"]  # moved: the read-only Mini check, no script
        try:
            done = edge.run([*verifier, "--minimum", version], capture_output=True, text=True,
''',
        "the proof that the Mini runs the release is `sm release verify --mini --minimum X`; its last line is read as before",
    ),
    (
        TRAIN,
        '''        listed = edge.run(["hz-ci-defer", "--list"], capture_output=True, text=True, check=False).stdout
''',
        '''        found = edge.run([*edge.sm(), "ci", "deferred"], capture_output=True, text=True, check=False)  # moved: `sm ci deferred`, no script
        listed = found.stdout if found.returncode == 0 else None  # moved: an unreadable list is not an empty one
''',
        "the deferred runs are listed with `sm ci deferred` (one line per run, nothing when none); a failed call is not an empty list",
    ),
    (
        TRAIN,
        '''        if wait_for_incident(self):
            return None
        if not listed.strip():
            return "nothing deferred"
''',
        '''        if wait_for_incident(self):
            return None
        if listed is None:  # moved: say so instead of noting a false "nothing deferred"
            return f"the list of deferred runs is unreadable (exit {found.returncode}); resume by hand: sm ci rerun"
        if not listed.strip():
            return "nothing deferred"
''',
        "a failed `sm ci deferred` is noted as unreadable, never as nothing deferred",
    ),
    (
        TRAIN,
        '''            done = edge.run(["hz-ci-defer", "--resume"], capture_output=True, text=True, check=False)
''',
        '''            done = edge.run([*edge.sm(), "ci", "rerun", "--all"], capture_output=True, text=True, check=False)  # moved: no script
''',
        "the opt-out resume of everything is `sm ci rerun --all` (`sm ci defer` has no --resume)",
    ),
    (
        TRAIN,
        '''            done = edge.run(["hz-ci-resume-after-tag"], capture_output=True, text=True, check=False)
''',
        '''            done = edge.run([*edge.sm(), "release", "train", "resume-ci"], capture_output=True, text=True, check=False)  # moved: no script
''',
        "the selective resume after a tag is `sm release train resume-ci`",
    ),
    (
        TRAIN,
        '''        edge.run(["hz-note", f"hz-train: {text}"], capture_output=True, check=False)
''',
        '''        edge.note(f"release train: {text}")  # moved: the same line in the same document, written here
''',
        "the note script is not started; the line is written by the edge, and it names the release train, not a tool",
    ),
    (
        TRAIN,
        '''        command = os.environ.get("HZ_TRAIN_NOTIFY") or next(iter(sorted(glob.glob(str(
            HOME / ".claude/plugins/synced/*/telegram-bridge/skills/telegram-notify/scripts/send.sh")))), "")
        sent = bool(command) and edge.run([command, text], capture_output=True, check=False).returncode == 0
''',
        '''        sent = edge.notify_owner(text)  # moved: the owner is reached through the notifier provider, no script is searched or started
''',
        "the owner notification goes through the notifier provider instead of a script found by a glob",
    ),
    (
        TRAIN,
        '''

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
''',
        "\n",
        "no second way in: the module is reached through its native commands only (the review gate sees every entry)",
    ),
    (
        NEXT,
        """HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
""",
        "",
        "siblings are imported relatively; nothing is put on sys.path",
    ),
    (
        NEXT,
        """        done = edge.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=ENV)
""",
        """        done = edge.run(edge.argv(cmd), capture_output=True, text=True, timeout=timeout, env=ENV)  # moved: an argument list, no shell
""",
        "a command line is split into an argument list and started by the core's runner; `sm` is this program",
    ),
    (
        NEXT,
        """    for n in queued_prs(sh("hz-queue list --active")):
""",
        """    for n in queued_prs(sh("sm queue list --active")):  # moved: the queue's listing
""",
        "the queued PRs are read from `sm queue list --active`, whose rows are the moved listing's",
    ),
    (
        NEXT,
        """    blocked = blockers(sh("hz-tick --digest", timeout=180), held)
""",
        """    blocked = blockers(edge.digest(sh), held)  # moved: `sm go --view tick`; unreadable is an UNKNOWN blocker
""",
        ("the digest is `sm go --view tick`; when it gives nothing (not native yet, or failed) the view lists an UNKNOWN blocker "
         "instead of none"),
    ),
    (
        NEXT,
        """    nudge = nudges(sh("hz-refill"))
""",
        """    nudge = nudges(sh("sm beads refill"))  # moved: the refill view
""",
        "builders to nudge come from `sm beads refill`",
    ),
    (
        NEXT,
        """        go = sh("hz-announce-merge").strip()
""",
        """        go = ""  # moved: the GO message tool is retired (tool map); its callers fold into go and tick
""",
        "the GO message tool is retired: nothing is announced, the rest of --start is unchanged",
    ),
    (
        NEXT,
        """            print(sh("hz-queue add " + " ".join(str(n) for n in green)).strip())
""",
        """            print(sh("sm queue add " + " ".join(str(n) for n in green)).strip())  # moved: the queue's add
""",
        "green PRs are queued with `sm queue add`",
    ),
    (
        PLAN,
        """HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
from ..queue_lib import known_failures as d
""",
        """from . import digest as d  # moved: the definitions of the digest module this table needs
""",
        "siblings are imported relatively; the digest definitions are this area's extract (queue_lib's has only the registry)",
    ),
    (
        PLAN,
        """LIST = os.path.join(HERE, "..", "research", "release-a.txt")  # one PR number per word; '#' starts a comment line
""",
        """LIST = "research/release-a.txt"  # moved: in the records checkout (edge.records_dir()); one PR number per word; '#' starts a comment line
""",
        "the candidate list is read from the configured records checkout, when it is read",
    ),
    (
        PLAN,
        """        text = open(LIST, encoding="utf-8").read()
""",
        """        text = open(edge.records_dir() / LIST, encoding="utf-8").read()
""",
        "the candidate list is read from the configured records checkout",
    ),
    (
        PLAN,
        """        print(f"no PRs: pass numbers or list them in {os.path.normpath(LIST)}")
""",
        """        print(f"no PRs: pass numbers or list them in {edge.records_dir() / LIST}")
""",
        "the message names the list in the configured records checkout",
    ),
    (
        PLAN,
        """    known = d.load_known(os.path.join(HERE, "..", "research", "known-failures.toml"))
""",
        """    known = d.load_known(str(edge.records_dir() / "research" / "known-failures.toml"))
""",
        "the KNOWN registry is read from the configured records checkout",
    ),
    (
        PLAN,
        """

if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["merged-since"] and len(a) == 2:
        sys.exit(merged_since(a[1]))
    if a[:1] == ["plan"]:
        sys.exit(plan(a[1:]))
    print(__doc__ + "\\nusage: hz_plan.py plan [PR...] | merged-since HH:MM")
    sys.exit(0 if a[:1] in (["-h"], ["--help"]) else 2)
""",
        "\n",
        "no second way in: the table is reached through `sm release train plan` only",
    ),
]
# Moved tests of this area: source test file -> new basename (unique repo-wide).
TESTS: dict[str, str] = {
    "test_train.py": "test_native_train_self_test.py",
    "test_pipelined_train.py": "test_native_train_pipelined.py",
    "test_train_main_gate.py": "test_native_train_main_gate.py",
    "test_next_start.py": "test_native_train_next_start.py",
    "test_plan.py": "test_native_train_plan.py",
}
TEST_PATCHES: list[tuple[str, str, str, str]] = [
    (
        "test_train.py",
        """    done = subprocess.run([sys.executable, str(TOOLS / "hz-train"), "--self-test"], capture_output=True, text=True,
""",
        """    done = subprocess.run([sys.executable, "-c", "import sys; from steuermaschine_heinzel.native.train_lib import train; sys.exit(train.main(sys.argv[1:]))", "--self-test"], capture_output=True, text=True,
""",
        "the child process imports the moved module and calls its main instead of starting the script by its path",
    ),
    (
        "test_train.py",
        """    done = subprocess.run([sys.executable, str(TOOLS / "hz-train"), "--cancel"], capture_output=True, text=True,
""",
        """    done = subprocess.run([sys.executable, "-c", "import sys; from steuermaschine_heinzel.native.train_lib import train; sys.exit(train.main(sys.argv[1:]))", "--cancel"], capture_output=True, text=True,
""",
        "the child process imports the moved module and calls its main instead of starting the script by its path",
    ),
    (
        "test_train_main_gate.py",
        """    train = subprocess.run([sys.executable, str(TOOLS / "hz-train"), "--help"], capture_output=True, text=True, timeout=30, env=env)
""",
        """    train = subprocess.run([sys.executable, "-c", "import sys; from steuermaschine_heinzel.native.train_lib import train; sys.exit(train.main(sys.argv[1:]))", "--help"], capture_output=True, text=True, timeout=30, env=env)
""",
        "the child process imports the moved module and calls its main instead of starting the script by its path",
    ),
    (
        "test_train_main_gate.py",
        """    assert '"--candidate-gate"' in (TOOLS / "hz-train").read_text()
""",
        """    assert '"--candidate-gate"' in (TOOLS.parent / "train_lib" / "train.py").read_text()
""",
        "the source that is read is the moved module",
    ),
    (
        "test_pipelined_train.py",
        """def test_migration_runs_only_under_the_train_lock_and_never_on_dry_run(train):
    src = (Path(__file__).resolve().parents[1] / "hz-train").read_text()
""",
        """def test_migration_runs_only_under_the_train_lock_and_never_on_dry_run(train):
    src = Path(train.__file__).read_text()
""",
        "the source that is read is the moved module",
    ),
    (
        "test_pipelined_train.py",
        """    src = (Path(__file__).resolve().parents[1] / "hz-train").read_text()
    drive = src[src.index("    def drive(self"):src.index("    def latest_tag")]
    assert "start_new_session=True" in drive and '"run", version' in drive
""",
        """    src = Path(train.__file__).read_text()
    drive = src[src.index("    def drive(self"):src.index("    def latest_tag")]
    assert "start_new_session=True" in drive and "edge.train_run(version)" in drive
""",
        "the source that is read is the moved module, and the detached runner is `sm daemon train --run X` (edge.train_run)",
    ),
    (
        "test_pipelined_train.py",
        """        assert Path(argv[0]).name == "hz-verify-mini" and argv[1:] == ["--minimum", version]
""",
        """        assert argv == [*train.edge.sm(), "release", "verify", "--mini", "--minimum", version]
""",
        "the Mini proof is `sm release verify --mini --minimum X`",
    ),
    (
        "test_pipelined_train.py",
        """        assert argv == ["hz-ci-defer", "--list"]
""",
        """        assert argv == [*train.edge.sm(), "ci", "deferred"]
""",
        "the deferred runs are listed with `sm ci deferred`",
    ),
    (
        "test_next_start.py",
        """        if cmd.startswith("hz-queue list"):
""",
        """        if cmd.startswith("sm queue list"):
""",
        "the queued PRs are read from `sm queue list --active`",
    ),
    (
        "test_next_start.py",
        """    assert not any(c.startswith(("hz-queue add", "hz-announce-merge")) for c in calls) and not list(tmp_path.iterdir())
""",
        """    assert not any(c.startswith(("sm queue add", "hz-announce-merge")) for c in calls) and not list(tmp_path.iterdir())
""",
        "green PRs are queued with `sm queue add`",
    ),
    (
        "test_next_start.py",
        """    assert "next: v0.9.32 batch open, 1 merged, 1 queued" in out and "GO 8" in out
    assert "hz-queue add 7" in calls
""",
        """    assert "next: v0.9.32 batch open, 1 merged, 1 queued" in out and "GO 8" not in out and not any(c.startswith("hz-announce-merge") for c in calls)
    assert "sm queue add 7" in calls
""",
        "the GO message tool is retired: --start announces nothing and never calls it; green PRs are queued with `sm queue add`",
    ),
]
FIXTURES: tuple[str, ...] = ()
EDGE_LINES = 44
TEST_EDGE_LINES = 20
# Lines the rules and patches remove from the moved module (a patch that only deletes adds no line to the count above).
EDGE_REMOVED_LINES = 57
