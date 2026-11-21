"""The release-main route of the focused admission: one reviewed release main manifest, fresh or paused recovery.

Moved from the reviewed isolated operator copy of the former tool (its `hz_focused_slots.py`): the release block
(`release_snapshot`, `release_guard`, `prepare_release`, `apply_release`), `read_release_nonconsumers`, its
`check_pr_holds` (with `require_owner`) and the three `Runtime` methods that pin the GitHub host. Everything else is
imported, unchanged, from the PR admission (`admission_lib.focused_slots`): the port plan's resolution of the
duplicated `check_consumers` is "keep the installed one".

tests/admission_verbatim.py checks that each moved block is in this file byte for byte. `main` is the argv form for
the moved tests; the commands are in native/release_admit.py.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import uuid

from ..admission_lib import focused_slots as _slots
from ..admission_lib import receipt_guard
from ..admission_lib.focused_slots import (  # noqa: F401  (the namespace the moved code and its tests expect)
    NAMES, STUCK_ASSERTION, STUCK_KIND, a, check_budget, check_consumers, check_preopen, digest, read_stuck_queued,
    reader_contract, require, resulting, update, version_locks)
from . import focused_release as release
from . import release_identity as hzlib
from steuermaschine.config import child_environment
from ... import state as statedir  # the plugin's state paths
from ..queue_lib import core_facts


class Runtime(_slots.Runtime):
    """The PR admission's boundary with the GitHub host pinned (operator copy: `__init__`, `run`, `set_variable`)."""
    def __init__(self, core, home=None, *, github_host=None):
        self.core = Path(core).resolve()
        self.home = Path.home() if home is None else Path(home)
        self.cache = statedir.root()
        self.management = self.home / 'Code/heinzel-management'
        self.github_host = github_host
        self.held_version_locks = None
        self.queue_baseline = None
        self.repo = a._repository_from_remote(self.core)
        self.domain = f'gui/{os.getuid()}'
        label = _slots.queue_label()  # None: queue_state refuses, naming the setting
        self.service = f'{self.domain}/{label}' if label else None
        self.plist = self.home / 'Library/LaunchAgents' / f'{label}.plist' if label else None

    def run(self, *args, data=None, check=True, timeout=30):
        options = {}
        if args[0] == 'gh' and self.github_host is not None:
            options['env'] = child_environment(GH_HOST=self.github_host)
            options['env'].pop('GH_REPO', None)
            if args[1] == 'api':
                args = (*args, '--hostname', self.github_host)
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout, **options)
        if check and result.returncode:
            raise a.AdmissionError(f'{args[0]} operation failed; inspect outcome before retry')
        return result


    def set_variable(self, name, value):
        require(name in NAMES[1:], 'only one compressed slot may be appended')
        repo = self.repo if self.github_host is None else f'{self.github_host}/{self.repo}'
        self.run('gh', 'variable', 'set', name, '-R', repo, data=value.encode())


def check_pr_holds(rt, pr, *, require_owner=True):
    actions = tomllib.loads(a._stable_file(rt.management / 'tools/pr-actions.toml', a.MAX_REVIEW_BYTES).decode())
    known = False
    for key, action in actions.items():
        hold = action.get('hold', False) if isinstance(action, dict) else None
        try:
            repo, number, _ = hzlib.split_key(key)
        except (ValueError, IndexError):
            require(hold is False, 'unrecognized potential hold entry')
            continue
        if repo.lower() == rt.repo.lower() and int(number) == pr:
            known = True
            require(hold is False, 'PR is held or hold is malformed')
    require(known or not require_owner, 'PR ownership entry unavailable')


def read_release_nonconsumers(rt, receipts):
    """Release-main-only accepts exactly the reviewed stuck-queued-pr-run kind; anything else refuses."""
    require(isinstance(receipts, list), 'invalid nonconsumer receipts')
    found = {}
    for receipt in receipts:
        require(isinstance(receipt, dict) and receipt.get('kind') == STUCK_KIND,
                'release-main-only accepts only stuck-queued-pr-run nonconsumers')
        run_id = receipt.get('run')
        require(type(run_id) is int and run_id > 0 and run_id not in found, 'invalid nonconsumer run identity')
        found[run_id] = read_stuck_queued(rt, receipt)
    return found



def release_snapshot(rt):
    repo, org = rt.variables()
    require(not set(NAMES) & org.keys(), 'organization focused transport changed')
    return {name: repo.get(name, '') for name in NAMES}, sorted(set(NAMES) & repo.keys())


def release_guard(rt, packet, review_root, workflow, desired, *, paused, exceptions=None, full=True):
    source, _ = release.source_proof(rt, packet, review_root)
    repo_id = release.remote_identity(rt, packet)
    source_info = packet['release']
    root = f'repos/{rt.repo}'
    policy = core_facts.at(source_info['head'])
    ci = rt.api(f'{root}/actions/workflows/{policy.workflow_file}')
    require(type(ci['id']) is int and ci['id'] == workflow and ci['path'] == policy.workflow
            and ci['state'] == 'active', 'canonical release CI workflow changed')
    require(not rt.pages(f"{root}/actions/workflows/{workflow}/runs?head_sha={source_info['head']}", 'workflow_runs'),
            'prospective release candidate CI already exists')
    recovery = source_info.get('recovery')
    recovery_proof = None
    if recovery is None:
        check_preopen(rt, source_info)
    else:
        check_pr_holds(rt, recovery['pr'], require_owner=False)
        recovery_proof = release.recovery_proof(rt, packet, review_root, workflow)
    require(not hzlib.merge_frozen(), 'merge freeze or release hold is set')
    if paused:
        require(rt.held_version_locks is not None and version_locks(rt.cache) == rt.held_version_locks,
                'version lock inventory changed')
    state = rt.queue_state()
    require(state is None if paused else state is not None and 'state = running' in state, 'queue exclusion changed')
    check_consumers(rt, exceptions or {}, desired, full=full)
    return dict(source=source, repository_id=repo_id, recovery=recovery_proof, budget_bytes=check_budget(rt, desired))


def prepare_release(rt, packet_path, review_root, nonconsumers=()):
    release.verify_account(rt)
    packet, raw = release.load_packet(packet_path, review_root)
    manifest, info = packet['manifest'], packet['release']
    release.require_no_attempt(rt, info)
    receipts = list(nonconsumers)
    exceptions = read_release_nonconsumers(rt, receipts)
    source_proof, raw_diff = release.source_proof(rt, packet, review_root)
    repo_id = release.remote_identity(rt, packet)
    base = info['base']
    workspace, snapshot = a._prepare_core_snapshot(rt.core, base)
    source = None
    try:
        source, blob, repo, variable, protected_files = a._canonical_source(snapshot, base)
        reader_contract(snapshot, base)
        require(repo == rt.repo and variable == NAMES[0], 'trusted transport identity changed')
        release.validate_manifest(packet, snapshot, source, protected_files, raw_diff)
        workflow = a._literal((source / 'scripts/focused_gate.py').read_bytes(), 'CI_WORKFLOW_ID')
        require(type(workflow) is int and workflow > 0, 'trusted canonical CI identity unavailable')
        expected, presence = release_snapshot(rt)
        key = a.registry_key(manifest)
        proposal = a._isolated_slot_append(source, expected[NAMES[0]], tuple(expected[name] for name in NAMES[1:]), {key: manifest})
        require(proposal['result'] is not None, 'release manifest already admitted; no new write required')
        index, candidate = proposal['result']
        slot = NAMES[index + 1]
        desired = {**expected, slot: candidate}
        guard = release_guard(rt, packet, review_root, workflow, desired, paused=False, exceptions=exceptions, full=False)
        require(guard['source'] == source_proof and guard['repository_id'] == repo_id, 'release source changed during prepare')
        required = {} if guard['recovery'] is None else guard['recovery']['required_entry']
        old_entries = resulting.release_registry_proof(source, manifest, expected, desired, required)
        require(release_snapshot(rt) == (expected, presence), 'focused variable snapshot changed during prepare')
        release.require_no_attempt(rt, info)
        proof = dict(mode=release.MODE, version=info['version'], head=info['head'], base=base, tree=info['tree'],
                     main_manifest_sha256=digest(a.canonical(manifest)), workflow=workflow, guard=guard, old_entries=old_entries)
        prepared = dict(schema=1, repository=repo, core_repo=str(rt.core), main=base, head=info['head'],
                        packet_sha256=digest(raw), validator_blob=blob, release_main=proof,
                        expected=expected, desired=desired, presence=presence, after_presence=sorted(set(presence) | {slot}),
                        slot=slot, candidate_sha256=digest(candidate.encode()))
        if receipts:
            prepared['nonconsumers'] = receipts
        return prepared, packet, source, workspace
    except BaseException:
        if source is not None:
            shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def apply_release(rt, prepared, packet, source, approval, review_root):
    require(approval == digest(a.canonical(prepared)), 'exact prepared transaction approval required')
    proof, info, manifest = prepared['release_main'], packet['release'], packet['manifest']
    require(packet['mode'] == proof['mode'] == release.MODE and prepared['repository'] == rt.repo
            and prepared['core_repo'] == str(rt.core)
            and all(proof[key] == info[key] for key in ('version', 'head', 'base', 'tree')),
            'invalid release-only transaction binding')
    release.require_no_attempt(rt, info)

    exceptions = read_release_nonconsumers(rt, prepared.get('nonconsumers', []))

    def guard(*, paused=True, full=False):
        require(release_guard(rt, packet, review_root, proof['workflow'], prepared['desired'], paused=paused,
                              exceptions=exceptions, full=full) == proof['guard'],
                'approved release eligibility or budget changed')

    guard(paused=False)
    require(release_snapshot(rt) == (prepared['expected'], prepared['presence']), 'approved release variable snapshot changed')
    required = {} if proof['guard']['recovery'] is None else proof['guard']['recovery']['required_entry']
    after_write = False
    observation = None
    pre_write = receipt_guard.pre_write(lambda: after_write)

    def read_registry():
        nonlocal observation
        values, presence = release_snapshot(rt)
        hashes = {name: digest(value.encode()) for name, value in values.items()}
        if after_write:
            observation = dict(transaction_sha256=approval, hashes=hashes, presence=presence)
            release.write_once(release.journal_path(rt, info, 'observed'), observation)
        require(presence == prepared['after_presence' if after_write else 'presence'], 'focused variable presence changed')
        return values

    def write_slot(name, value):
        nonlocal after_write
        require(name == prepared['slot'] and value == prepared['desired'][name], 'approved release slot or bytes changed')
        release.write_once(release.journal_path(rt, info, 'attempt'), dict(transaction_sha256=approval,
            repository=rt.repo, version=info['version'], head=info['head'], slot=name,
            candidate_sha256=prepared['candidate_sha256'], presence=prepared['presence'],
            before={key: digest(raw.encode()) for key, raw in prepared['expected'].items()}))
        rt.set_variable(name, value)
        after_write = True

    def check_append(before, after):
        require(after == prepared['desired'] and
                resulting.release_registry_proof(source, manifest, before, after, required) == proof['old_entries'],
                'approved release one-entry append changed')

    receipt = dict(schema=1, admission_id=uuid.uuid4().hex, repository=rt.repo, transaction_sha256=approval,
                   packet_sha256=prepared['packet_sha256'], manifest_sha256=proof['main_manifest_sha256'],
                   release_main=proof, **{key: info[key] for key in ('version', 'head', 'base', 'tree')})
    with rt.exclusions():
        receipt = a.apply_slots([manifest], source, prepared['expected'], read_registry, write_slot, receipt,
                                rt.cache / 'focused-pr-registry.lock', rt.home,
                                approved_candidate_sha256=prepared['candidate_sha256'],
                                check_fresh=lambda: guard(full=pre_write()), release_main=proof, check_main_append=check_append)
    state = rt.queue_state()
    require(state is not None and 'state = running' in state, 'queue restoration unverified; operator action required')
    require(observation is not None and observation['hashes'] == receipt['after'], 'exact release readback was not observed')
    receipt.update(queue_restored=True, readback=observation['hashes'], presence=observation['presence'],
                   slot=prepared['slot'], before_presence=prepared['presence'])
    return a._write_receipt(receipt, rt.home)



def main(argv=None):
    """The argv form of the release route (the release branch of the operator copy's `main`), for the moved tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'apply'))
    parser.add_argument('packet', type=Path)
    parser.add_argument('--core-repo', type=Path, required=True)
    parser.add_argument('--review-root', type=Path, required=True)
    parser.add_argument('--reviewed-protected-producer', action='store_true')
    parser.add_argument('--pr', type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--expected-remote-head', help='explicit old full SHA for a reviewed prospective fast-forward PR')
    mode.add_argument('--resulting-main-only', action='store_true', help='admit one reviewed resulting-main plan for an unchanged green PR')
    mode.add_argument('--release-main-only', action='store_true', help='admit one reviewed fresh or paused recovery release main plan')
    parser.add_argument('--packets', nargs='+')
    parser.add_argument('--core-repos', nargs='+')
    parser.add_argument('--prs', nargs='+')
    parser.add_argument('--stuck-queued', action='store_true')
    parser.add_argument('--cancel-blocking-pr-runs', action='store_true')
    parser.add_argument('--nonconsumers', type=Path)
    parser.add_argument('--prepared', type=Path, help='exact previously reviewed prepare output; mandatory for apply')
    parser.add_argument('--approved-sha256', help='SHA256 of canonical prepared JSON; mandatory for apply')
    args = parser.parse_args(argv)
    source = workspace = None
    print(json.dumps({'event': 'focused_admission.start', 'operation': args.operation}), file=sys.stderr)
    try:
        require(args.release_main_only, 'this entry admits release-main-only; there is no PR admission any more')
        require(args.pr is None and not args.reviewed_protected_producer,
                'release-main-only excludes PR and protected producer options')
        require(not (args.packets or args.core_repos or args.prs or args.stuck_queued or args.cancel_blocking_pr_runs),
                'release-main-only excludes batch, stuck-queued generation and run cancelling')
        require((args.operation == 'apply') == bool(args.prepared and args.approved_sha256), 'apply needs both exact prepared file and approval')
        require(sys.flags.isolated, 'release-main-only requires python3 -I on the reviewed entry point')
        rt = Runtime(args.core_repo, github_host='github.com')
        receipts = [] if args.nonconsumers is None else a._json_bytes(a._stable_file(args.nonconsumers, a.MAX_REVIEW_BYTES), 'nonconsumers')['receipts']
        prepared, packet, source, workspace = prepare_release(rt, args.packet, args.review_root, nonconsumers=receipts)
        if args.operation == 'prepare':
            print(a.canonical(prepared).decode())
        else:
            approved = a._json_bytes(a._stable_file(args.prepared, 1024 * 1024), 'prepared transaction')
            require(a.canonical(approved) == a.canonical(prepared), 'prepared transaction changed; reprepare and review')
            print(apply_release(rt, prepared, packet, source, args.approved_sha256, args.review_root))
        print(json.dumps({'event': 'focused_admission.outcome', 'operation': args.operation, 'result': 'success'}), file=sys.stderr)
        return 0
    except (a.AdmissionError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'event': 'focused_admission.outcome', 'operation': args.operation, 'result': 'refused', 'reason': str(error)}), file=sys.stderr)
        return 1
    finally:
        if source is not None:
            shutil.rmtree(source, ignore_errors=True)
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)
