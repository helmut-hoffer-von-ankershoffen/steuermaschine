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
from . import repair_main as repair
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


def check_pr_holds(rt, pr):
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
    # added (m598): a PR `sm head pr` opened or adopted on this machine is owned without an entry; an entry still decides first
    known = known or pr_owned.owned(rt.repo, pr, rt.cache) is not None
    require(known, 'PR ownership entry unavailable (neither the hold file nor an `sm head pr` record names it)')


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


def fresh_guard(rt, packet, desired, exceptions, pr, paused, *, pr_update=None, snapshot=None, resulting_main=None,
                probe=True, full=False, repair_main=None):
    packet_guard(rt, packet, pr, pr_update=pr_update, snapshot=snapshot, resulting_main=resulting_main, repair_main=repair_main)
    global_guard(rt, desired, exceptions, paused, probe=probe, full=full)


def packet_guard(rt, packet, pr, *, pr_update=None, snapshot=None, resulting_main=None, repair_main=None):
    """Per-packet freshness: main, PR identity and holds or pre-open proof, local reviewed head."""
    plan = packet['manifest']
    if repair_main is not None:
        # The repaired main is the merge of this head; its PR is closed, so no PR identity or hold applies.
        require(pr is None and resulting_main is None and pr_update is None, 'repair-main mode conflicts with transaction')
        repair.guard(rt, packet, repair_main)
    elif rt.api(f'repos/{rt.repo}/git/ref/heads/main')['object']['sha'] != plan['base']:
        require(False, 'main changed')
    elif resulting_main is not None:
        require(resulting_main['mode'] == resulting.MODE and pr == packet['original']['pr'],
                'resulting-main mode conflicts with transaction')
        require(resulting.live_proof(rt, packet) == resulting_main['live'], 'original PR eligibility or evidence changed')
        check_pr_holds(rt, pr)
    elif pr_update is not None:
        update.validate_record(pr_update)
        require(pr == pr_update['pr'] and plan['head'] == pr_update['new'], 'typed PR update conflicts with transaction')
        update.authenticate(rt, plan, pr, pr_update['old'], snapshot, expected=pr_update)
        check_pr_holds(rt, pr)
    elif pr:
        pull = rt.api(f'repos/{rt.repo}/pulls/{pr}')
        require(pull.get('state') == 'open' and pull.get('head', {}).get('sha') == plan['head']
                and pull.get('base', {}).get('repo', {}).get('full_name') == rt.repo
                and pull.get('base', {}).get('ref') == 'main', 'PR identity changed')
        check_pr_holds(rt, pr)
    else:
        check_preopen(rt, plan)
    head = packet['original']['head'] if resulting_main is not None else plan['head']
    require(a._run(['git', 'rev-parse', 'HEAD'], cwd=rt.core).decode().strip() == head, 'local reviewed head changed')


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


def prepare(rt, packet_path, review_root, protected=False, pr=None, nonconsumers=(), expected_remote_head=None,
            resulting_main_only=False, *, probe=True, repair_main=False):
    require(not environment.state_overrides().any(),
            'coordinator state overrides are not supported for live admission')
    require(not repair_main or (pr is None and expected_remote_head is None and not protected and not resulting_main_only),
            'repair-main takes the packet only: no PR, update, resulting-main or protected producer option')
    if resulting_main_only:
        require(type(pr) is int and pr > 0 and expected_remote_head is None and not protected,
                'resulting-main-only requires a PR and excludes update and protected producer options')
        packet, raw = resulting.load_packet(packet_path, review_root, pr)
        head = packet['original']['head']
    else:
        raw = a._stable_file(packet_path, a.MAX_PACKET_BYTES)
        head = a._json_bytes(raw, 'packet')['manifest']['head']
        packet, _, _ = a.load_packet(packet_path, review_root, head, source_pair=True)
        if expected_remote_head is not None:
            update.validate_option(pr, expected_remote_head, head)
    base = a._read_main(rt.repo)
    if resulting_main_only:
        require(packet['manifest']['base'] == base, 'resulting-main manifest base is stale')
    elif repair_main:
        require(packet['main_manifest']['base'] != base, 'repair-main plans main itself, on its parent: the packet base is current main')
    else:
        a.validate_live_binding(packet, head, base)
    workspace, snapshot = a._prepare_core_snapshot(rt.core, base)
    source = None
    try:
        # Source and all validators/codecs come from authenticated main in the disposable clone.
        source, blob, repo, variable, protected_files = a._canonical_source(snapshot, base)
        reader_contract(snapshot, base)
        require(repo == rt.repo and variable == NAMES[0], 'trusted transport identity changed')
        pr_update = (update.authenticate(rt, packet['manifest'], pr, expected_remote_head, snapshot)
                     if expected_remote_head is not None else None)
        for is_main in ((True,) if resulting_main_only or repair_main else (False, True)):
            item = {**packet, 'manifest': packet['main_manifest']} if is_main and not resulting_main_only else packet
            a.validate_canonical(item, snapshot, base, repo, variable, protected_files, source,
                                 resulting_main=is_main, protected_producer=protected)
        if resulting_main_only:
            require(not {'scripts/focused_gate.py', 'scripts/focused_gate.py.lock', 'scripts/merge_gate.py',
                         'scripts/merge_gate.py.lock', resulting._policy(rt, base).judge_file} & packet['manifest']['changed'].keys(),
                    'resulting-main-only refuses protected judgment source changes')
        repo_variables, org_variables = rt.variables()
        require(not set(NAMES) & org_variables.keys(), 'organization focused transport ambiguity')
        presence = sorted(set(NAMES) & repo_variables.keys())
        expected = {name: repo_variables.get(name, '') for name in NAMES}
        main_proof = repair_proof = None
        if repair_main:
            repair_proof = repair.prove(rt, packet, snapshot, source, base, expected)
        if resulting_main_only:
            check_pr_holds(rt, pr)
            live = resulting.live_proof(rt, packet)
            original_plan = resulting.registry_proof(source, packet, expected)
            replay = resulting.replay_original(rt, packet, snapshot, original_plan, live)
            main_proof = dict(mode=resulting.MODE, base=base, head=head, tree=packet['manifest']['tree'],
                              original=packet['original'], main_manifest_sha256=digest(a.canonical(packet['manifest'])),
                              live=live, original_replay=replay)
        plans = ([packet['manifest']] if resulting_main_only else [packet['main_manifest']] if repair_main
                 else [packet['manifest'], packet['main_manifest']])
        proposal = a._isolated_slot_append(source, expected[NAMES[0]], tuple(expected[n] for n in NAMES[1:]),
                                           {a.registry_key(p): p for p in plans})
        desired = dict(expected)
        result = proposal['result']
        require(result is not None, 'reviewed scope already admitted; no new write required')
        index, candidate = result
        desired[NAMES[index + 1]] = candidate
        if resulting_main_only:
            resulting.registry_proof(source, packet, expected, desired)
        if repair_main:
            repair.append_proof(source, packet, expected, desired)
        # Fetch proof source into disposable snapshot, never mutate caller refs/worktree.
        proof_rt = Runtime(snapshot, rt.home)
        proofs = read_nonconsumers(proof_rt, nonconsumers, review_root)
        fresh_guard(rt, packet, desired, proofs, pr, paused=False, pr_update=pr_update, snapshot=snapshot,
                    resulting_main=main_proof, probe=probe, repair_main=repair_proof)
        prepared = dict(schema=1, repository=repo, main=base, head=head, packet_sha256=digest(raw),
                        validator_blob=blob, protected_producer=protected, pr=pr,
                        expected=expected, desired=desired, presence=presence, slot=NAMES[index + 1],
                        candidate_sha256=digest(candidate.encode()), nonconsumers=list(nonconsumers))
        if pr_update is not None:
            prepared['pr_update'] = pr_update
        if main_proof is not None:
            prepared['resulting_main'] = main_proof
        if repair_proof is not None:
            prepared['repair_main'] = repair_proof
        return prepared, packet, source, workspace, proofs
    except BaseException:
        if source is not None:
            shutil.rmtree(source, ignore_errors=True)
        shutil.rmtree(workspace, ignore_errors=True)
        raise


def registry_io(rt, prepared):
    """Five-value reader pinned to the prepared presence inventory, plus the one slot writer."""
    after_write = False
    def read_registry():
        repo, org = rt.variables()
        require(not set(NAMES) & org.keys(), 'organization focused transport changed')
        presence = set(prepared['presence']) | ({prepared['slot']} if after_write else set())
        require(set(NAMES) & repo.keys() == presence, 'focused variable presence changed')
        return {n: repo.get(n, '') for n in NAMES}
    def write_slot(name, value):
        nonlocal after_write
        rt.set_variable(name, value)
        after_write = True
    # Third value: whether the slot was written. After the write the guard still re-reads
    # everything but sends no cancel POST, so an API refusal there cannot strand a
    # written slot without its receipt.
    return read_registry, write_slot, lambda: after_write


def apply_prepared(rt, prepared, packet, source, proofs, approval, *, snapshot=None):
    require(approval == digest(a.canonical(prepared)), 'exact prepared transaction approval required')
    # Mandatory concrete guard includes complete consumer and variable inventory each time.
    pr_update = prepared.get('pr_update')
    main_proof = prepared.get('resulting_main')
    repair_proof = prepared.get('repair_main')
    if 'repair_main' in prepared:
        require(pr_update is None and main_proof is None and prepared.get('pr') is None and isinstance(repair_proof, dict)
                and repair_proof.get('mode') == repair.MODE and repair_proof.get('head') == packet['manifest']['head']
                and repair_proof.get('main_manifest_sha256') == digest(a.canonical(packet['main_manifest'])),
                'invalid repair-main transaction binding')
    if 'resulting_main' in prepared:
        require(pr_update is None and isinstance(main_proof, dict) and main_proof.get('mode') == resulting.MODE
                and packet.get('mode') == resulting.MODE and main_proof.get('original') == packet['original'],
                'invalid resulting-main transaction binding')
    if 'pr_update' in prepared:
        update.validate_record(pr_update)
        require(snapshot is not None, 'PR update needs its disposable source snapshot')
    read_registry, write_slot, written = registry_io(rt, prepared)
    full = receipt_guard.pre_write(written)
    guard = lambda: fresh_guard(rt, packet, prepared['desired'], proofs, prepared['pr'], paused=True,
                               pr_update=pr_update, snapshot=snapshot, resulting_main=main_proof, probe=not written(),
                               full=full(), repair_main=repair_proof)
    with rt.exclusions():
        receipt = dict(schema=1, admission_id=uuid.uuid4().hex, head=prepared['head'],
                       transaction_sha256=approval, packet_sha256=prepared['packet_sha256'])
        if pr_update is not None:
            receipt['pr_update'] = pr_update
        kwargs = {}
        if main_proof is not None:
            receipt['resulting_main'] = main_proof
            kwargs = dict(resulting_main=main_proof,
                          check_main_append=lambda before, after: resulting.registry_proof(source, packet, before, after))
        if repair_proof is not None:
            receipt['repair_main'] = repair_proof
            kwargs = dict(repair_main=repair_proof,
                          check_main_append=lambda before, after: repair.append_proof(source, packet, before, after))
        plans = ([packet['manifest']] if main_proof is not None else [packet['main_manifest']] if repair_proof is not None
                 else [packet['manifest'], packet['main_manifest']])
        return a.apply_slots(plans, source, prepared['expected'],
                             read_registry, write_slot, receipt, rt.cache / 'focused-pr-registry.lock', rt.home,
                             approved_candidate_sha256=prepared['candidate_sha256'], check_fresh=guard, **kwargs)


BATCH_MODE = 'batch/v1'
# Core append_slot places the whole batch in the first slot it fits; an empty
# 40 kB slot holds ~68 entries (~34 pairs). 20 pairs keeps a margin and bounds
# the per-guard API work (each member re-proves its identity at every guard).
MAX_BATCH = 20


def batch_guard(rt, members, desired, exceptions, paused, *, probe=True, full=False):
    """Every member's own packet guard, then the transaction-wide guard exactly once."""
    for member_rt, packet, pr in members:
        packet_guard(member_rt, packet, pr)
    global_guard(rt, desired, exceptions, paused, probe=probe, full=full)


def prepare_batch(rt, entries, review_root, nonconsumers=(), *, probe=True):
    """Many reviewed PR+main pairs as ONE transaction: one proposal, one approval, one write.

    entries: (packet_path, core_checkout, pr_or_None) per member. Each member keeps the
    full single-packet validation: reviewed pair packet with pinned review hash, exact
    head, base equal to current main, both canonical resolutions, and its own owned Core
    checkout at the reviewed head with pre-open proof or strict --pr identity and holds.
    Update, resulting-main-only and protected-producer modes stay single-packet. Core
    append_slot is atomic: every pair lands in one slot or nothing is proposed.
    """
    require(not environment.state_overrides().any(),
            'coordinator state overrides are not supported for live admission')
    require(2 <= len(entries) <= MAX_BATCH, f'batch admission needs 2..{MAX_BATCH} packets; use the single-packet path for one')
    members, raws = [], []
    for packet_path, core, pr in entries:
        require(pr is None or (type(pr) is int and pr > 0), 'PR must be positive')
        member_rt = Runtime(core, rt.home)
        require(member_rt.repo == rt.repo, 'batch member checkout is not the official Core repository')
        raw = a._stable_file(packet_path, a.MAX_PACKET_BYTES)
        head = a._json_bytes(raw, 'packet')['manifest']['head']
        packet, _, _ = a.load_packet(packet_path, review_root, head, source_pair=True)
        members.append((member_rt, packet, pr))
        raws.append(raw)
    heads = [packet['manifest']['head'] for _, packet, _ in members]
    prs = [pr for _, _, pr in members if pr is not None]
    require(len(set(heads)) == len(heads) and len({m.core for m, _, _ in members}) == len(members)
            and len(set(prs)) == len(prs), 'batch members need distinct heads, Core checkouts and PRs')
    base = a._read_main(rt.repo)
    for _, packet, _ in members:
        a.validate_live_binding(packet, packet['manifest']['head'], base)
    workspaces, snapshots, source = [], [], None
    try:
        # One disposable clone per member checkout, so each reviewed head is resolved from its own objects.
        for member_rt, _, _ in members:
            workspace, snapshot = a._prepare_core_snapshot(member_rt.core, base)
            workspaces.append(workspace)
            snapshots.append(snapshot)
        source, blob, repo, variable, protected_files = a._canonical_source(snapshots[0], base)
        reader_contract(snapshots[0], base)
        require(repo == rt.repo and variable == NAMES[0], 'trusted transport identity changed')
        for (_, packet, _), snapshot in zip(members, snapshots, strict=True):
            for is_main in (False, True):
                item = {**packet, 'manifest': packet['main_manifest']} if is_main else packet
                a.validate_canonical(item, snapshot, base, repo, variable, protected_files, source,
                                     resulting_main=is_main, protected_producer=False)
        repo_variables, org_variables = rt.variables()
        require(not set(NAMES) & org_variables.keys(), 'organization focused transport ambiguity')
        presence = sorted(set(NAMES) & repo_variables.keys())
        expected = {name: repo_variables.get(name, '') for name in NAMES}
        plans = [plan for _, packet, _ in members for plan in (packet['manifest'], packet['main_manifest'])]
        additions = {a.registry_key(plan): plan for plan in plans}
        require(len(additions) == len(plans), 'batch members share a registry key; admit them separately')
        try:
            proposal = a._isolated_slot_append(source, expected[NAMES[0]], tuple(expected[n] for n in NAMES[1:]), additions)
        except a.AdmissionError as error:
            # Core tried every slot for the whole batch (or found a conflict/partial presence).
            raise a.AdmissionError(f'batch of {len(members)} pairs refused by trusted-main slot append, nothing '
                                   f'proposed; if no slot fits, split the batch: {error}') from error
        result = proposal['result']
        require(result is not None, 'every batch pair already admitted; no new write required')
        index, candidate = result
        desired = dict(expected)
        desired[NAMES[index + 1]] = candidate
        proofs = read_nonconsumers(Runtime(snapshots[0], rt.home), nonconsumers, review_root)
        batch_guard(rt, members, desired, proofs, paused=False, probe=probe)
        prepared = dict(schema=1, mode=BATCH_MODE, repository=repo, main=base, validator_blob=blob,
                        protected_producer=False,
                        members=[dict(head=head, packet_sha256=digest(raw), pr=pr)
                                 for head, raw, (_, _, pr) in zip(heads, raws, members, strict=True)],
                        expected=expected, desired=desired, presence=presence, slot=NAMES[index + 1],
                        candidate_sha256=digest(candidate.encode()), nonconsumers=list(nonconsumers))
        return prepared, members, source, workspaces, proofs
    except BaseException:
        if source is not None:
            shutil.rmtree(source, ignore_errors=True)
        for workspace in workspaces:
            shutil.rmtree(workspace, ignore_errors=True)
        raise


def apply_batch(rt, prepared, members, source, proofs, approval):
    """Apply one approved batch: one queue pause, one lock set, one slot write, one readback."""
    require(approval == digest(a.canonical(prepared)), 'exact prepared transaction approval required')
    require(prepared.get('mode') == BATCH_MODE and len(prepared.get('members', ())) == len(members)
            and all(m['head'] == packet['manifest']['head'] and m['pr'] == pr
                    for m, (_, packet, pr) in zip(prepared['members'], members, strict=True)),
            'invalid batch transaction binding')
    read_registry, write_slot, written = registry_io(rt, prepared)
    full = receipt_guard.pre_write(written)
    guard = lambda: batch_guard(rt, members, prepared['desired'], proofs, paused=True, probe=not written(), full=full())
    with rt.exclusions():
        # Receipt file name uses the first member head; every member is listed inside.
        receipt = dict(schema=1, admission_id=uuid.uuid4().hex, head=prepared['members'][0]['head'],
                       mode=BATCH_MODE, members=prepared['members'], transaction_sha256=approval)
        plans = [plan for _, packet, _ in members for plan in (packet['manifest'], packet['main_manifest'])]
        return a.apply_slots(plans, source, prepared['expected'], read_registry, write_slot, receipt,
                             rt.cache / 'focused-pr-registry.lock', rt.home,
                             approved_candidate_sha256=prepared['candidate_sha256'], check_fresh=guard,
                             pairs=len(members))


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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'apply', 'blockers', 'nonconsumers'))
    parser.add_argument('--stuck-queued', action='store_true',
                        help='nonconsumers only: print stuck-queued-pr-run receipts for the current blockers')
    parser.add_argument('packet', type=Path, nargs='?')
    parser.add_argument('--core-repo', type=Path, help='single packet and blockers: the owned Core checkout')
    parser.add_argument('--review-root', type=Path, required=True)
    batch = parser.add_argument_group('batch', 'many reviewed pairs in one transaction (pre-open or strict --pr members)')
    batch.add_argument('--packets', type=Path, nargs='+')
    batch.add_argument('--core-repos', type=Path, nargs='+', help='one owned Core checkout per packet, at its reviewed head')
    batch.add_argument('--prs', type=int, nargs='+', help='PR per packet in the same order; 0 = pre-open (default)')
    parser.add_argument('--reviewed-protected-producer', action='store_true')
    parser.add_argument('--pr', type=int)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--expected-remote-head', help='explicit old full SHA for a reviewed prospective fast-forward PR')
    mode.add_argument('--resulting-main-only', action='store_true', help='admit one reviewed resulting-main plan for an unchanged green PR')
    parser.add_argument('--nonconsumers', type=Path)
    parser.add_argument('--prepared', type=Path, help='exact previously reviewed prepare output; mandatory for apply')
    parser.add_argument('--approved-sha256', help='SHA256 of canonical prepared JSON; mandatory for apply')
    args = parser.parse_args(argv)
    source = workspace = None
    workspaces = []
    print(json.dumps({'event': 'focused_admission.start', 'operation': args.operation}), file=sys.stderr)
    try:
        require(args.pr is None or args.pr > 0, 'PR must be positive')
        require(not args.resulting_main_only or (args.pr is not None and not args.reviewed_protected_producer),
                'resulting-main-only requires a PR and excludes protected producer approval')
        if args.expected_remote_head is not None:
            update.validate_option(args.pr, args.expected_remote_head)
        require((args.operation == 'apply') == bool(args.prepared and args.approved_sha256), 'apply needs both exact prepared file and approval')
        receipts = [] if args.nonconsumers is None else a._json_bytes(a._stable_file(args.nonconsumers, a.MAX_REVIEW_BYTES), 'nonconsumers')['receipts']
        require(args.stuck_queued == (args.operation == 'nonconsumers'), 'nonconsumers needs --stuck-queued, which belongs to it')
        if args.operation == 'nonconsumers':
            require(args.core_repo is not None and args.packet is None and not args.packets and args.pr is None,
                    'nonconsumers takes --stuck-queued, --core-repo, --review-root and optional --nonconsumers only')
            output, excluded = stuck_queued_receipts(Runtime(args.core_repo), {receipt['run'] for receipt in receipts})
            print(json.dumps(output, indent=1, sort_keys=True))
            print(json.dumps({'event': 'focused_admission.outcome', 'operation': args.operation, 'result': 'success',
                              'receipts': len(output['receipts']), 'excluded': excluded}), file=sys.stderr)
            return 0
        if args.operation == 'blockers':
            require(args.core_repo is not None and args.packet is None and not args.packets and args.pr is None,
                    'blockers takes --core-repo, --review-root and optional --nonconsumers only')
            report = report_blockers(Runtime(args.core_repo), {receipt['run'] for receipt in receipts})
            print(a.canonical(report).decode())
            print(json.dumps({'event': 'focused_admission.outcome', 'operation': args.operation, 'result': 'success',
                              'blocking': len(report['blocking'])}), file=sys.stderr)
            return 0
        if args.packets:
            require(args.packet is None and args.core_repo is None and args.pr is None and args.expected_remote_head is None
                    and not args.resulting_main_only and not args.reviewed_protected_producer,
                    'batch takes --packets/--core-repos/--prs; update, resulting-main and protected producer stay single-packet')
            prs = args.prs if args.prs is not None else [0] * len(args.packets)
            require(args.core_repos is not None and len(args.core_repos) == len(args.packets) == len(prs)
                    and all(number >= 0 for number in prs), 'batch needs one --core-repos checkout and one --prs number (0 = pre-open) per packet')
            rt = Runtime(args.core_repos[0])
            entries = [(path, core, number or None) for path, core, number in zip(args.packets, args.core_repos, prs, strict=True)]
            prepared, members, source, workspaces, proofs = prepare_batch(rt, entries, args.review_root, receipts)
            if args.operation == 'prepare':
                print(a.canonical(prepared).decode())
            else:
                approved = a._json_bytes(a._stable_file(args.prepared, 1024 * 1024), 'prepared transaction')
                require(approved == prepared, 'prepared transaction changed; reprepare and review')
                print(apply_batch(rt, prepared, members, source, proofs, args.approved_sha256))
            print(json.dumps({'event': 'focused_admission.outcome', 'operation': args.operation, 'result': 'success',
                              'pairs': len(entries)}), file=sys.stderr)
            return 0
        require(args.packet is not None and args.core_repo is not None and not args.core_repos and not args.prs,
                'single-packet mode needs PACKET and --core-repo')
        rt = Runtime(args.core_repo)
        prepared, packet, source, workspace, proofs = prepare(rt, args.packet, args.review_root,
            protected=args.reviewed_protected_producer, pr=args.pr, nonconsumers=receipts,
            expected_remote_head=args.expected_remote_head, resulting_main_only=args.resulting_main_only)
        if args.operation == 'prepare':
            print(a.canonical(prepared).decode())
        else:
            approved = a._json_bytes(a._stable_file(args.prepared, 1024 * 1024), 'prepared transaction')
            require(approved == prepared, 'prepared transaction changed; reprepare and review')
            print(apply_prepared(rt, prepared, packet, source, proofs, args.approved_sha256,
                                 snapshot=workspace / 'repo'))
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
        for item in workspaces:
            shutil.rmtree(item, ignore_errors=True)
