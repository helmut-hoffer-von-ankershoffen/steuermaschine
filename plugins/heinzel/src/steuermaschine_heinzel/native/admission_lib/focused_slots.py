"""Guarded compressed focused registry admission; prepare is read-only, apply is exact-pinned.

No retirement, migration, generic writer, automatic rollback or remote CAS is provided.
Nonconsumer exceptions require independently reviewed immutable source receipts, never names.
"""
from __future__ import annotations

import argparse
import base64
import calendar
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import tomllib
import uuid
import zlib

from . import focused_admit as a
from . import focused_resulting as resulting
from . import focused_update as update
from . import identity as hzlib
from . import receipt_guard
from ... import environment  # moved: the process context is a typed model
from ... import state as statedir  # the plugin's state paths
from .. import pr_owned  # added (m598): PRs opened by sm head pr
from ..queue_lib import core_facts

NAMES = ('FOCUSED_CI_PLANS', *(f'FOCUSED_CI_PLANS_V2_{i}' for i in range(4)))
ACTIVE = ('queued', 'in_progress', 'waiting', 'pending', 'requested')
BUDGET = 256 * 1024
MAX_VAR = 48 * 1024
QUEUE_LABEL_SETTING = 'queue_service_label'  # the launchd label of the running queue service; no default (a machine's value)
QUEUE_LABEL_UNSET = (f'the queue service label is not set: set {QUEUE_LABEL_SETTING} to the launchd label that runs '
                     '`sm daemon queue` here (`launchctl list | grep queue`); nothing was paused')


def queue_label():
    """The setting queue_service_label inside a native command; None outside one or when it is not set."""
    from steuermaschine.native import runtime
    try:
        values = runtime().settings
    except RuntimeError:
        return None
    values = values.of('heinzel') if hasattr(values, 'of') else values
    label = (values if isinstance(values, dict) else dict(values)).get(QUEUE_LABEL_SETTING)
    return str(label) if label else None
FORBIDDEN = re.compile(rb'FOCUSED_CI_PLANS|focused_plan|focused_gate|focused_coverage|hz-focused')

# Zombie pull_request runs (GitHub Actions outage): queued, never assigned a job,
# uncancellable (409) and undeletable. Such a run reads nothing: registry variables
# are injected only into a job that starts. If GitHub ever starts it, its focused
# reader looks up exactly `pull_request:<head_sha>`; because that head is proven
# absent from the live AND the desired registry, the lookup misses and the run fails
# closed ("No focused plan"), before and after this one slot write alike. Every
# receipt binds one exact run; every guard re-proves all of this from fresh reads.
STUCK_KIND = 'stuck-queued-pr-run'
STUCK_ASSERTION = 'queued pull_request run without jobs; its head has no focused plan'
STUCK_PATHS = frozenset(f'.github/workflows/{name}.yml' for name in ('ci', 'codeql', 'claude-review', 'docs'))
STUCK_IDENTITY = frozenset(('path', 'head_sha', 'head_branch', 'event', 'run_attempt', 'workflow_id', 'created_at'))
STUCK_MIN_AGE = 30 * 60
STUCK_CANCEL_409 = b'Cannot cancel a workflow run that has not been queued yet'
STUCK_TIME = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(ok, message):
    if not ok:
        raise a.AdmissionError(message)


class Runtime:
    """Concrete local and GitHub boundary; tests replace this entire IO boundary."""
    def __init__(self, core, home=None):
        self.core = Path(core).resolve()
        self.home = Path.home() if home is None else Path(home)
        self.cache = statedir.root()
        self.management = self.home / 'Code/heinzel-management'
        self.held_version_locks = None
        self.queue_baseline = None
        self.repo = a._repository_from_remote(self.core)
        self.domain = f'gui/{os.getuid()}'
        label = queue_label()  # None: queue_state refuses, naming the setting
        self.service = f'{self.domain}/{label}' if label else None
        self.plist = self.home / 'Library/LaunchAgents' / f'{label}.plist' if label else None

    def run(self, *args, data=None, check=True, timeout=30):
        result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise a.AdmissionError(f'{args[0]} operation failed; inspect outcome before retry')
        return result

    def api(self, path):
        return a._json_bytes(self.run('gh', 'api', path).stdout, 'GitHub response')

    def pages(self, path, key):
        rows, total, page = [], None, 1
        per_page = 30 if key == 'variables' else 100
        max_pages = 40 if key == 'variables' else 20
        while True:
            require(page <= max_pages, 'inventory exceeds bounded pagination')
            obj = self.api(f'{path}{"&" if "?" in path else "?"}per_page={per_page}&page={page}')
            count, batch = obj.get('total_count'), obj.get(key)
            require(type(count) is int and count >= 0 and isinstance(batch, list), 'incomplete inventory')
            require(total is None or total == count, 'inventory changed during pagination')
            total = count
            rows.extend(batch)
            require(len(rows) <= total, 'inventory count mismatch')
            if len(rows) == total:
                return rows
            require(len(batch) == per_page, 'truncated inventory')
            page += 1

    def variables(self):
        identity = self.api(f'repos/{self.repo}')
        owner = identity.get('owner')
        owner_name, repo_name = self.repo.split('/')
        require(identity.get('full_name') == self.repo and identity.get('name') == repo_name
                and isinstance(owner, dict) and owner.get('login') == owner_name
                and owner.get('type') in ('User', 'Organization'), 'repository owner identity unavailable or inconsistent')
        repo = self.pages(f'repos/{self.repo}/actions/variables', 'variables')
        # Only the authenticated personal-owner identity proves org inheritance is inapplicable.
        # Organization inventory errors remain fatal; HTTP422 is never treated as absence.
        org = (self.pages(f'repos/{self.repo}/actions/organization-variables', 'variables')
               if owner['type'] == 'Organization' else [])
        def values(rows):
            result = {}
            for row in rows:
                name, value = row.get('name'), row.get('value')
                require(isinstance(name, str) and re.fullmatch(r'[A-Z_][A-Z_0-9]*', name)
                        and name not in result and isinstance(value, str), 'invalid variable inventory')
                result[name] = value
            return result
        return values(repo), values(org)

    def registry(self):
        repo, org = self.variables()
        require(not set(NAMES) & org.keys(), 'focused transport must not be shadowed by organization variables')
        return {name: repo.get(name, '') for name in NAMES}

    def now(self):
        return time.time()

    def cancel_probe(self, run_id):
        """POST cancel for one exact run; a stuck run answers 409. Never retried."""
        result = self.run('gh', 'api', '-X', 'POST', f'repos/{self.repo}/actions/runs/{run_id}/cancel', check=False)
        return result.returncode, result.stdout + result.stderr

    def set_variable(self, name, value):
        require(name in NAMES[1:], 'only one compressed slot may be appended')
        self.run('gh', 'variable', 'set', name, '-R', self.repo, data=value.encode())

    def queue_state(self, *, timeout=30):
        require(self.service is not None and self.plist is not None, QUEUE_LABEL_UNSET)
        result = self.run('launchctl', 'print', self.service, check=False, timeout=timeout)
        if result.returncode:
            # Only the documented missing-service response proves it is stopped.
            require(b'Could not find service' in result.stderr, 'queue state unavailable')
            return None
        text = result.stdout.decode()
        require(f'path = {self.plist}' in text, 'unexpected queue LaunchAgent source')
        return text

    def restore_queue(self):
        require(self.queue_baseline is not None and a._stable_file(self.plist, 1024 * 1024) == self.queue_baseline,
                'original queue plist changed; restoration refused')
        end = time.monotonic() + 8
        while True:
            state = self.queue_state()
            if state is not None and 'state = running' in state:
                return
            if state is None:
                self.run('launchctl', 'bootstrap', self.domain, str(self.plist), check=False)
            if time.monotonic() >= end:
                raise a.AdmissionError('queue restoration unverified; operator action required')
            time.sleep(.2)

    def wait_queue_pause(self):
        """Observe one asynchronous bootout; no locks or writes before proven absence."""
        deadline = time.monotonic() + 8
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, 'queue pause unverified within 8 seconds')
            require(a._stable_file(self.plist, 1024 * 1024) == self.queue_baseline,
                    'original queue plist changed during pause')
            state = self.queue_state(timeout=min(3, remaining))
            require(time.monotonic() <= deadline, 'queue pause unverified within 8 seconds')
            require(a._stable_file(self.plist, 1024 * 1024) == self.queue_baseline,
                    'original queue plist changed during pause')
            if state is None:
                return
            time.sleep(min(.2, max(0, deadline - time.monotonic())))

    @contextmanager
    def exclusions(self):
        baseline = self.queue_state()
        require(baseline is not None and 'state = running' in baseline, 'queue must be running before controlled pause')
        self.queue_baseline = a._stable_file(self.plist, 1024 * 1024)
        # Set the restore obligation before bootout: a failed command may have taken effect.
        try:
            self.run('launchctl', 'bootout', self.service)
            self.wait_queue_pause()
            with ExitStack() as stack:
                for relative in ('queue.lock', 'train/train.lock', 'release.lock'):
                    stack.enter_context(a._lock(self.cache / relative))
                train = self.cache / 'train'
                # The dispatcher lock prevents new train runners. Existing runners own version locks.
                require(not train.is_symlink(), 'unsafe train state directory')
                versions = version_locks(self.cache)
                for path in versions:
                    require(re.fullmatch(r'v\d+\.\d+\.\d+\.lock', path.name), 'unknown train lock')
                    stack.enter_context(a._lock(path))
                self.held_version_locks = versions
                yield
        finally:
            self.held_version_locks = None
            self.restore_queue()


def version_locks(cache):
    result = []
    for parent in (cache / 'train', cache / 'release'):
        require(not parent.is_symlink(), 'unsafe version lock directory')
        try:
            children = list(parent.iterdir())
        except FileNotFoundError:
            continue
        result.extend(path for path in children if path.name.startswith('v') and path.name.endswith('.lock'))
    return sorted(result)


def reader_contract(core, base):
    policy = core_facts.at(base) if a.SHA.fullmatch(str(base)) else core_facts.at_main()
    for path, count in ((policy.workflow, 2), (policy.judge_workflow, 1)):
        text = a._run(['git', 'show', f'{base}:{path}'], cwd=core).decode()
        for name in NAMES:
            require(text.count(f'{name}: ${{{{ vars.{name} }}}}') == count, 'trusted main lacks all registry reader injections')
    gate = a._run(['git', 'show', f'{base}:scripts/focused_gate.py'], cwd=core)
    require(b'registry_slots' in gate and b'selected_registry' in gate, 'protected reader not adopted')


def check_budget(rt, desired):
    repo, org = rt.variables()
    require(not set(NAMES) & org.keys(), 'organization focused transport ambiguity')
    repo.update({k: v for k, v in desired.items() if v or k in repo})
    require(len(repo) <= 500 and len(org) <= 1000, 'variable count limit')
    # Count names too; retain even shadowed org values conservatively.
    require(all(len(v.encode()) <= MAX_VAR for v in (*repo.values(), *org.values())), 'individual variable limit')
    size = sum(len(k.encode()) + len(v.encode()) for source in (repo, org) for k, v in source.items())
    require(size <= BUDGET, 'repository/organization transport budget exceeded')
    return size


def read_nonconsumers(rt, receipts, review_root):
    """Authenticate reviewed immutable closure receipts; completeness is a review obligation."""
    found = {}
    for receipt in receipts:
        run_id = receipt.get('run')
        require(type(run_id) is int and run_id > 0 and run_id not in found, 'invalid nonconsumer run identity')
        if receipt.get('kind') == STUCK_KIND:
            found[run_id] = read_stuck_queued(rt, receipt)
            continue
        require('kind' not in receipt, 'unknown nonconsumer kind')
        require(receipt.get('repository') == rt.repo and receipt.get('assertion') == 'complete immutable source closure has no focused registry reads',
                'nonconsumer needs explicit complete-closure review')
        review = receipt['review']
        path = review.get('path')
        require(isinstance(path, str) and not Path(path).is_absolute() and '..' not in Path(path).parts, 'unsafe nonconsumer review path')
        target = review_root / path
        require(target.resolve().is_relative_to(review_root.resolve()), 'nonconsumer review escapes root')
        require(digest(a._stable_file(target, a.MAX_REVIEW_BYTES)) == review['sha256'], 'nonconsumer review drift')
        pin = receipt['identity']
        require(isinstance(pin, dict) and type(pin.get('run_attempt')) is int and pin['run_attempt'] > 0
                and type(pin.get('workflow_id')) is int and pin['workflow_id'] > 0, 'invalid nonconsumer workflow attempt')
        require(isinstance(pin.get('path'), str) and pin['path'].startswith('.github/workflows/')
                and isinstance(pin.get('head_branch'), str) and bool(pin['head_branch']), 'invalid nonconsumer workflow ref')
        require(pin.get('event') in ('schedule', 'workflow_dispatch'), 'unsupported immutable nonconsumer event')
        commit = a._sha(pin.get('head_sha'), 'nonconsumer source commit')
        paths = receipt.get('source')
        require(isinstance(paths, dict) and pin.get('path') in paths and paths, 'missing workflow closure inventory')
        require(isinstance(receipt.get('referenced_workflows'), list), 'missing transitive workflow inventory')
        a._run(['git', 'fetch', '--no-tags', 'origin', commit], cwd=rt.core)
        for path, blob in paths.items():
            require(isinstance(path, str) and not path.startswith('-') and not Path(path).is_absolute()
                    and '..' not in Path(path).parts, 'invalid closure path')
            a._sha(blob, 'nonconsumer source blob')
            actual = a._run(['git', 'rev-parse', f'{commit}:{path}'], cwd=rt.core).decode().strip()
            require(actual == blob, 'nonconsumer closure source changed')
            mode = a._run(['git', 'ls-tree', commit, '--', path], cwd=rt.core).split()[0]
            require(mode in (b'100644', b'100755'), 'nonconsumer source is not a regular file')
            contents = a._run(['git', 'show', f'{commit}:{path}'], cwd=rt.core)
            require(not FORBIDDEN.search(contents), 'nonconsumer closure reads focused registry')
        found[run_id] = receipt
    return found


def read_stuck_queued(rt, receipt):
    """Structural check of one stuck-queued receipt; liveness is re-proved by every guard."""
    pin = receipt.get('identity')
    require(set(receipt) == {'kind', 'run', 'repository', 'assertion', 'identity'}
            and receipt.get('repository') == rt.repo and receipt.get('assertion') == STUCK_ASSERTION
            and isinstance(pin, dict) and set(pin) == STUCK_IDENTITY, 'invalid stuck-queued receipt')
    a._sha(pin['head_sha'], 'stuck-queued head')
    branch = pin['head_branch']
    require(pin['event'] == 'pull_request' and pin['path'] in STUCK_PATHS
            and isinstance(branch, str) and branch and branch != 'main'
            and not branch.startswith(('release/', 'bump/', 'refs/'))
            and type(pin['run_attempt']) is int and pin['run_attempt'] > 0
            and type(pin['workflow_id']) is int and pin['workflow_id'] > 0
            and isinstance(pin['created_at'], str) and STUCK_TIME.fullmatch(pin['created_at']),
            'stuck-queued receipt is outside the pull_request allowlist')
    return receipt


def registry_text(values):
    """Canonical JSON of the legacy registry and every decoded slot; undecodable refuses."""
    parts = []
    for name in NAMES:
        value = values[name]
        require(isinstance(value, str), 'focused registry value unreadable')
        try:
            if name != NAMES[0] and value:
                envelope = json.loads(value)
                require(isinstance(envelope, dict) and envelope.get('codec') == 'gzip+base64'
                        and type(envelope.get('bytes')) is int and 0 < envelope['bytes'] <= 1024 * 1024
                        and isinstance(envelope.get('data'), str), 'undecodable focused slot')
                decoder = zlib.decompressobj(wbits=31)
                value = decoder.decompress(base64.b64decode(envelope['data'], validate=True), envelope['bytes'] + 1)
                require(len(value) == envelope['bytes'] and decoder.eof and not decoder.unused_data
                        and not decoder.unconsumed_tail, 'undecodable focused slot')
            parsed = json.loads(value or '{}')
        except (ValueError, zlib.error) as error:
            raise a.AdmissionError('undecodable focused registry; stuck-queued proof refused') from error
        require(isinstance(parsed, dict), 'undecodable focused registry')
        parts.append(json.dumps(parsed, sort_keys=True))
    return '\n'.join(parts)


def stuck_queued_failure(rt, run, pin, registries, *, probe=True):
    """None when this exact run is still a harmless zombie, else the refusal reason."""
    if any(run.get(k) != v for k, v in pin.items()) or run.get('id') is None:
        return 'identity changed'
    if run.get('status') != 'queued' or run.get('conclusion') is not None:
        return 'run left queued'
    if not (isinstance(run.get('repository'), dict) and run['repository'].get('full_name') == rt.repo):
        return 'repository changed'
    if run.get('event') != 'pull_request' or run.get('path') not in STUCK_PATHS:
        return 'outside the pull_request allowlist'
    # Age counts from GitHub's (re)queue time; missing or malformed counts as too young.
    started = run.get('run_started_at')
    if not (isinstance(started, str) and STUCK_TIME.fullmatch(started)) or \
            rt.now() - calendar.timegm(time.strptime(started, '%Y-%m-%dT%H:%M:%SZ')) < STUCK_MIN_AGE:
        return 'younger than 30 minutes'
    jobs = rt.api(f'repos/{rt.repo}/actions/runs/{run["id"]}/jobs?per_page=100')
    # Zero jobs means no runner received the workflow, so nothing read any variable.
    if jobs.get('total_count') != 0 or jobs.get('jobs') != []:
        return 'run has jobs'
    # A head with a live or desired plan could consume one; refuse even a substring hit.
    if any(pin['head_sha'] in text for text in registries):
        return 'head has a focused plan'
    if probe:
        # Last, after every read check: a 409 proves GitHub still cannot cancel it.
        code, output = rt.cancel_probe(run['id'])
        if code == 0 or b'HTTP 409' not in output or STUCK_CANCEL_409 not in output:
            return 'cancel no longer answers 409'
    return None


def active_runs(rt, rows=None):
    """Two complete active-status scans; refuse unless membership and identity agree.

    `rows`, when given, receives the second scan's complete list row per run (receipt_guard reads them)."""
    def inventory(keep):
        found = {}
        for state in ACTIVE:
            rows = rt.pages(f'repos/{rt.repo}/actions/runs?status={state}', 'workflow_runs')
            for run in rows:
                run_id = run.get('id')
                require(type(run_id) is int and run_id > 0 and run_id not in found and run.get('status') == state,
                        'active run inventory changed or duplicated')
                found[run_id] = {k: run.get(k) for k in ('status', 'path', 'head_sha', 'head_branch', 'event', 'run_attempt', 'workflow_id')}
                if keep is not None:
                    keep[run_id] = run
        return found
    first, second = inventory(None), inventory(rows)
    require(first == second, 'active run membership drift; reprepare when stable')
    return second


def blocking_runs(active, exceptions):
    """Exact active runs without a reviewed nonconsumer receipt, sorted by run ID (report only)."""
    return [dict(id=run_id, **{k: active[run_id][k] for k in ('status', 'event', 'head_branch', 'path', 'head_sha')})
            for run_id in sorted(active) if run_id not in exceptions]


def check_consumers(rt, exceptions, desired=None, *, probe=True, full=True):
    """Every active run needs an exact receipt. `full`: the per-run proof below for every run, on bounded threads.
    Otherwise stuck-queued receipts are proved from their untouched list rows (receipt_guard); other kinds as in full."""
    rows = {}
    active = active_runs(rt, rows)
    blocking = blocking_runs(active, exceptions)
    registries = None
    # Same refusal as before, now naming the exact runs so the operator can act on them.
    require(not blocking, 'unknown active Actions run; admission refused: ' + '; '.join(
        f"{run['id']} {run['status']} {run['event']} {run['head_branch']} {run['path']}" for run in blocking[:20])
        + (f'; and {len(blocking) - 20} more' if len(blocking) > 20 else ''))
    if any(exceptions[run_id].get('kind') == STUCK_KIND for run_id in active):
        # Without the transaction's desired registry the head proof is incomplete.
        require(desired is not None, 'stuck-queued receipt needs the desired registry')
        registries = (registry_text(rt.registry()), registry_text(desired))
    if not full:
        gone = receipt_guard.vanished(active, exceptions, STUCK_KIND)
        require(not gone, 'stuck-queued run ' + ', '.join(map(str, gone))
                + ' no longer harmless: run vanished from the active inventory')
        for run_id in sorted(active):
            proof = exceptions[run_id]
            if proof.get('kind') == STUCK_KIND:
                reason = receipt_guard.row_failure(rt, rows[run_id], proof['identity'], registries, paths=STUCK_PATHS,
                                                   min_age=STUCK_MIN_AGE, stamp=STUCK_TIME)
                require(reason is None, f'stuck-queued run {run_id} no longer harmless: {reason}')
        others = [item for item in active.items() if exceptions[item[0]].get('kind') != STUCK_KIND]
    else:
        others = list(active.items())
    receipt_guard.parallel(lambda item: authenticate_run(rt, *item, exceptions[item[0]], registries, probe=probe), others)


def authenticate_run(rt, run_id, observed, proof, registries, *, probe=True):
    """The former per-run body of check_consumers, verbatim but for `continue` -> `return` and the hoisted registries."""
    run = rt.api(f'repos/{rt.repo}/actions/runs/{run_id}')
    require({k: run.get(k) for k in observed} == observed and run.get('id') == run_id,
            'active run changed during exact authentication')
    require(isinstance(run.get('repository'), dict) and run['repository'].get('full_name') == rt.repo,
            'active run repository changed')
    if proof.get('kind') == STUCK_KIND:
        reason = stuck_queued_failure(rt, run, proof['identity'], registries, probe=probe)
        require(reason is None, f'stuck-queued run {run_id} no longer harmless: {reason}')
        return
    require(all(run.get(k) == v for k, v in proof['identity'].items())
            and set(proof['identity']) == {'path', 'head_sha', 'head_branch', 'event', 'run_attempt', 'workflow_id'},
            'active nonconsumer identity changed')
    require(run.get('referenced_workflows') == proof['referenced_workflows'], 'missing or changed transitive workflow identity')




def check_preopen(rt, plan):
    branch = a._run(['git', 'branch', '--show-current'], cwd=rt.core).decode().strip()
    require(bool(branch), 'pre-open admission requires an owned named branch')
    def inventory():
        rows = []
        for page in range(1, 21):
            response = rt.run('gh', 'api', f'repos/{rt.repo}/pulls?state=open&per_page=100&page={page}')
            batch = json.loads(response.stdout, object_pairs_hook=a._unique_pairs)
            require(isinstance(batch, list) and len(batch) <= 100, 'invalid open PR inventory')
            for pull in batch:
                require(isinstance(pull.get('head'), dict) and pull.get('state') == 'open', 'incomplete open PR identity')
                require(pull['head'].get('sha') != plan['head'] and pull['head'].get('ref') != branch,
                        'matching open PR exists; supply --pr and honor its holds')
                rows.append((pull['number'], pull['head']['sha'], pull['head']['ref']))
            if len(batch) < 100:
                return sorted(rows)
        raise a.AdmissionError('open PR inventory exceeds bound')
    require(inventory() == inventory(), 'open PR inventory changed during pre-open proof')






def global_guard(rt, desired, exceptions, paused, *, probe=True, full=False):
    """Transaction-wide freshness: freeze, version locks, release holds, queue, consumers, budget."""
    freeze = rt.cache / 'merge-freeze'
    require(not freeze.exists() and not freeze.is_symlink(), 'merge freeze is set')
    if paused:
        require(rt.held_version_locks is not None and version_locks(rt.cache) == rt.held_version_locks, 'version lock inventory changed')
    holds = rt.cache / 'release-holds'
    require(not holds.is_symlink() and (not holds.exists() or not any(holds.iterdir())), 'release hold is set or unreadable')
    require((rt.queue_state() is None) == paused, 'queue exclusion changed')
    check_consumers(rt, exceptions, desired, probe=probe, full=full)
    check_budget(rt, desired)








def report_blockers(rt, claimed):
    """List the exact active runs that make apply refuse. Read-only: it cancels nothing (`sm ci defer` and `sm ci
    cancel` do, by their own allowlist). Runs named by a nonconsumer receipt (`claimed` IDs) are omitted here but are
    still fully authenticated by prepare/apply. Every allowlisted run is a blocker unless a stuck-queued-pr-run
    receipt names it."""
    return dict(blocking=blocking_runs(active_runs(rt), claimed))


def stuck_queued_receipts(rt, claimed):
    """Snapshot active unclaimed runs that are harmless zombies now into receipts for review.

    Read-only: the cancel probe (a POST) is left to prepare/apply. Runs that fail any
    read check are listed under `excluded` and stay blockers.
    """
    registries = (registry_text(rt.registry()),)
    receipts, excluded = [], []
    for row in blocking_runs(active_runs(rt), claimed):
        run = rt.api(f'repos/{rt.repo}/actions/runs/{row["id"]}')
        pin = {k: run.get(k) for k in sorted(STUCK_IDENTITY)}
        receipt = dict(kind=STUCK_KIND, run=row['id'], repository=rt.repo, assertion=STUCK_ASSERTION, identity=pin)
        try:
            read_stuck_queued(rt, receipt)
            reason = stuck_queued_failure(rt, run, pin, registries, probe=False)
        except a.AdmissionError as error:
            reason = str(error)
        if reason is None:
            receipts.append(receipt)
        else:
            excluded.append(dict(id=row['id'], reason=reason))
    return dict(receipts=receipts), excluded


