#!/usr/bin/env python3
"""Mutation checks for the guards of the admission port: each mutant weakens one guard; the named tests must fail.

usage (from the repository root): uv run python plugins/heinzel/tests/admission_mutants.py [TEST ...]
Default tests: the ops, wait and recorded test files and the module-entry test. A mutant whose tests do not end within
TIMEOUT seconds counts as killed (the "limit ends the wait" mutant loops forever by construction).
Edits the source files in place and restores them; do not run it while you edit them, and do not interrupt it.
"""
import subprocess
import sys
from pathlib import Path

TIMEOUT = 300
DEFAULT = ['plugins/heinzel/tests/test_native_admission_ops.py', 'plugins/heinzel/tests/test_native_admission_wait.py',
           'plugins/heinzel/tests/test_native_admission_recorded.py',
           'plugins/heinzel/tests/test_native_admission_slots.py::test_module_entry_is_not_a_second_door_to_the_write']
OPS = Path('plugins/heinzel/src/steuermaschine_heinzel/native/admission_lib/ops.py')
ADM = Path('plugins/heinzel/src/steuermaschine_heinzel/native/admission_lib/focused_admit.py')
SLO = Path('plugins/heinzel/src/steuermaschine_heinzel/native/admission_lib/focused_slots.py')
WAI = Path('plugins/heinzel/src/steuermaschine_heinzel/native/admission_lib/wait.py')
CMD = Path('plugins/heinzel/src/steuermaschine_heinzel/native/admission.py')
M = [
 ('approved digest must be the stored transaction digest', OPS, 's.require(canonical_sha256(approved_doc) == approved,', 's.require(True,'),
 ('approved must be 64 hex', OPS, 's.require(isinstance(approved, str) and a.HEX.fullmatch(approved) is not None,', 's.require(True,'),
 ('args sha256 bound to approval', OPS, 's.require(args.get("sha256") == approved,', 's.require(True,'),
 ('args pr verified', OPS, 's.require(args.get("pr") == approved_doc.get("pr"),', 's.require(True,'),
 ('args protected verified', OPS, 's.require(bool(args.get("protected")) == approved_doc.get("protected_producer"),', 's.require(True,'),
 ('args resulting-main verified', OPS, 's.require(bool(args.get("resulting_main_only")) == ("resulting_main" in approved_doc),', 's.require(True,'),
 ('args expected remote head verified', OPS, 's.require(args.get("expected_remote_head") == (approved_doc.get("pr_update") or {}).get("old"),', 's.require(True,'),
 ('args packet bytes verified', OPS,
  's.require(s.digest(a._stable_file(Path(args["packet"]), a.MAX_PACKET_BYTES)) == approved_doc.get("packet_sha256"),', 's.require(True,'),
 ('args nonconsumer receipts verified', OPS, 's.require(_receipts(nonconsumers) == approved_doc.get("nonconsumers"),', 's.require(True,'),
 ('batch transaction refused by single apply', OPS, 's.require("mode" not in approved_doc,', 's.require(True,'),
 ('worktree must be at the reviewed head', OPS, '    s.require(actual == head,', '    s.require(True,'),
 ('worktree check skipped', OPS, '        if not args.get("resulting_main_only"):\n            check_worktree_head', '        if False:\n            check_worktree_head'),
 ('stored args belong to this packet', OPS, 's.require(args.get("schema") == 1 and args.get("packet") == str(Path(packet).resolve()),', 's.require(True,'),
 ('missing args file refused', OPS, 's.require(path.is_file(),', 's.require(True,'),
 ('recomputed transaction must equal the reviewed one', OPS,
  's.require(approved_doc == prepared, "prepared transaction changed; reprepare and review")\n        receipt', 's.require(True, "x")\n        receipt'),
 ('PR must be positive', OPS, 's.require(pr is None or pr > 0, "PR must be positive")', 's.require(True, "PR must be positive")'),
 ('digest newline excluded (canonical form)', OPS, 'return hashlib.sha256(a.canonical(value)).hexdigest()', 'return hashlib.sha256(a.canonical(value) + b"\\n").hexdigest()'),
 ('repository identity must be OWNER/NAME', ADM, 'if not isinstance(value, str) or value.count("/") != 1:', 'if False:'),
 ('registry snapshot must equal expected (moved guard, sanity)', ADM, 'if current != expected:', 'if False:'),
 ('command prepare sends no probe', OPS, 'runtime: Any = None, probe: bool = False)', 'runtime: Any = None, probe: bool = True)'),
 ('moved prepare passes probe to its guard', SLO, 'resulting_main=main_proof, probe=probe)', 'resulting_main=main_proof, probe=True)'),
 ('exclusive modes refuse', OPS, 's.require(not (expected_remote_head is not None and resulting_main_only),', 's.require(True,'),
 ('wait: only truncation and drift are transient', WAI, 'return TRUNCATED in text or DRIFT in text', 'return True'),
 ('wait: limit ends the wait', WAI, 'if waited >= max_minutes:', 'if False:'),
 ('wait: prepare does not run while a run blocks', WAI, '        if rows:\n', '        if False:\n'),
 ('wait: other refusal in the poll is raised', WAI, '            if not transient(error):\n                raise\n            pause(f"a stable', '            pause(f"a stable'),
 ('command: process timeout is a refusal', CMD, 'TypeError, subprocess.SubprocessError)', 'TypeError)'),
 ('command: StillWaiting becomes exit 75', CMD, 'raise Wait(waiting.message, waiting.waiting_on) from waiting', 'raise Refuse(waiting.message) from waiting'),
 ('command: head apply is a write command', CMD, '@native("head apply", writes=True,', '@native("head apply", writes=False,'),
]
tests = sys.argv[1:] or DEFAULT
base = subprocess.run(['uv', 'run', 'pytest', '-q', '-x', '-p', 'no:cacheprovider', *tests], capture_output=True, text=True)
assert base.returncode == 0, 'baseline must be green: ' + base.stdout[-800:]
print('baseline green', flush=True)
killed = survived = 0
for name, path, old, new in M:
    text = path.read_text()
    assert text.count(old) == 1, (name, text.count(old))
    path.write_text(text.replace(old, new))
    try:
        try:
            ok = subprocess.run(['uv', 'run', 'pytest', '-q', '-x', '-p', 'no:cacheprovider', *tests], capture_output=True, text=True,
                                timeout=TIMEOUT).returncode != 0
        except subprocess.TimeoutExpired:
            ok = True
    finally:
        path.write_text(text)
    killed += ok
    survived += not ok
    print(('KILLED  ' if ok else 'SURVIVED'), name, flush=True)
print(f'{killed} killed, {survived} survived')
