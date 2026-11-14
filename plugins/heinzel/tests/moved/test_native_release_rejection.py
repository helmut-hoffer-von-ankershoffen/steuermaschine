"""Only isolated private state is ever used; no GitHub, cache or release mutation."""
import base64
import copy
import json
import os

import pytest


@pytest.fixture
def pending(tool, home, monkeypatch):
    rel = tool('hz-release')
    mod = rel.release_rejection
    rel.remember(version=mod.VERSION, compose='2026-11-05T10:00:00Z')
    rel.acquire_snapshot_freeze(mod.VERSION)
    raw = rel.paths()[1].read_bytes()
    args = [mod.VERSION, '--reject-release', '--failed-compose-run', '42020635931',
            '--failed-compose-attempt', '1', '--expected-receipt-sha256', mod.digest(raw),
            '--expected-freeze-token', rel.state()['freeze_token'], '--expected-compose-head', mod.HEAD,
            '--expected-core-source', mod.SOURCE, '--reason', 'Published SDK cannot compose; retain all published bytes.']
    actual_evidence = mod.evidence
    def evidence_bypass(_):
        return {'stable': 'exact identities', 'verified_provenance': ['verified']}
    evidence_bypass.original = actual_evidence
    monkeypatch.setattr(mod, 'evidence', evidence_bypass)
    actual_quiet = mod.quiet
    def quiet_bypass():
        return None
    quiet_bypass.original = actual_quiet
    monkeypatch.setattr(mod, 'quiet', quiet_bypass)
    return rel, mod, args, raw


def test_rejection_is_failure_preserves_original_and_foreign_holds(pending, capsys):
    rel, mod, args, raw = pending
    own = rel.snapshot_hold_path()
    other = own.parent / ('e' * 32)
    other.write_text('other')
    rel.paths()[0].write_text('incident')
    assert rel.main(args) == 1
    assert not own.exists()
    assert other.read_text() == 'other' and rel.paths()[0].read_text() == 'incident'
    assert rel.state()['release_status'] == 'REJECTED' and rel.state()['finished'] is True
    record = mod.records()[0]
    assert record['phase'] == 'complete'
    assert base64.b64decode(record['receipt_base64']) == raw
    assert (mod.history().stat().st_mode & 0o777) == 0o700
    assert ((mod.history() / (mod.VERSION + '.json')).stat().st_mode & 0o777) == 0o600
    assert rel.main(args) == 1
    assert '0.9.16 rejected; published Core retained; combined app not accepted' in capsys.readouterr().out


@pytest.mark.parametrize('flag,value', [('--expected-receipt-sha256', 'a' * 64), ('--expected-freeze-token', 'b' * 32),
                                         ('--expected-compose-head', 'c' * 40), ('--expected-core-source', 'd' * 40),
                                         ('--failed-compose-attempt', '0'), ('--failed-compose-run', '-1'), ('--reason', ' ')])
def test_wrong_identity_never_removes_hold(pending, flag, value):
    rel, mod, args, _ = pending
    args[args.index(flag) + 1] = value
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists()
    assert not mod.records()


@pytest.mark.parametrize('extra', [['--pause'], ['--prepare-only'], ['--snapshot-acceptance'], ['--intro', 'x'],
                                   ['--acceptance-ref', 'main'], ['--acceptance-ref', ''], ['--intro', ''],
                                   ['--replace-acceptance-run', '0'], ['--time=120'],
                                   ['--replace-acceptance-run', '3'], ['--timeout', '1']])
def test_mixed_flags_refused(pending, extra):
    rel, _, args, _ = pending
    with pytest.raises(SystemExit):
        rel.main(args + extra)
    assert rel.snapshot_hold_path().exists()


@pytest.mark.parametrize('field', ['finished', 'snapshot', 'acceptance_identity', 'unexpected'])
def test_unsafe_receipt_state_refused(pending, field):
    rel, mod, args, _ = pending
    rel.remember(**{field: True})
    args[args.index('--expected-receipt-sha256') + 1] = mod.digest(rel.paths()[1].read_bytes())
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists()


@pytest.mark.parametrize('boundary', ['archive', 'receipt-pending', 'authorize', 'unlink', 'receipt-final', 'complete'])
@pytest.mark.parametrize('after', [False, True])
def test_crash_boundaries_same_decision_recover(pending, monkeypatch, boundary, after):
    rel, mod, args, _ = pending
    original_persist, original_write, original_unlink = mod.persist, rel.write_state, rel.unlink_pinned_hold
    hit = False
    def crash(label, action):
        nonlocal hit
        if label == boundary and not hit:
            hit = True
            if after:
                action()
            raise OSError('simulated interrupted boundary')
        return action()
    def persist(fd, name, record, **kwargs):
        label = {'archived': 'archive', 'unlink-authorized': 'authorize', 'complete': 'complete'}[record['phase']]
        return crash(label, lambda: original_persist(fd, name, record, **kwargs))
    def write(data):
        label = 'receipt-pending' if data.get('release_status') == 'REJECTING' else 'receipt-final'
        return crash(label, lambda: original_write(data))
    def unlink(fd, identity):
        return crash('unlink', lambda: original_unlink(fd, identity))
    with monkeypatch.context() as scoped:
        scoped.setattr(mod, 'persist', persist)
        scoped.setattr(rel, 'write_state', write)
        scoped.setattr(rel, 'unlink_pinned_hold', unlink)
        assert rel.main(args) == 1
    assert hit
    if mod.records():
        with pytest.raises(mod.InvalidRejection):
            mod.guard('0.9.17') if mod.records()[0]['phase'] != 'complete' else mod.guard('0.9.16')
    assert rel.main(args) == 1
    assert mod.records()[0]['phase'] == 'complete'
    assert not rel.snapshot_hold_path().exists()


def test_changed_reason_replay_and_normal_dispatch_blocked(pending, monkeypatch):
    rel, mod, args, _ = pending
    monkeypatch.setattr(rel, 'unlink_pinned_hold', lambda *_: (_ for _ in ()).throw(OSError('crash')))
    assert rel.main(args) == 1
    old = copy.deepcopy(mod.records()[0])
    args[-1] = 'a different reason'
    assert rel.main(args) == 1
    assert mod.records()[0] == old
    monkeypatch.setattr(rel, 'release_plugin', lambda *_: pytest.fail('plugin dispatched'))
    monkeypatch.setattr(rel, 'release_core', lambda *_: pytest.fail('core dispatched'))
    assert rel.main(['--plugin', 'clock']) == 1
    assert rel.main(['0.9.17']) == 1


def test_tombstone_survives_later_receipt(pending, monkeypatch):
    rel, mod, args, _ = pending
    assert rel.main(args) == 1
    rel.write_state({'version': '0.9.17'})
    with pytest.raises(mod.InvalidRejection):
        mod.guard('0.9.16')
    mod.guard('0.9.17')
    monkeypatch.setattr(rel, 'release_core', lambda *a, **k: 37)
    assert rel.main(['0.9.17']) == 37  # normal path retained, no acceptance fabricated
    assert rel.main(['0.9.16']) == 1


@pytest.mark.parametrize('target', ['receipt-symlink', 'receipt-public', 'history-symlink', 'history-public', 'hold-symlink'])
def test_unsafe_files_fail_closed(pending, target, home):
    rel, mod, args, _ = pending
    receipt = rel.paths()[1]
    if target == 'receipt-symlink':
        alternate = receipt.with_name('alternate')
        receipt.rename(alternate)
        receipt.symlink_to(alternate)
    elif target == 'receipt-public':
        receipt.chmod(0o644)
    elif target == 'history-symlink':
        alternate = home / 'alternate'
        alternate.mkdir(mode=0o700)
        mod.history().symlink_to(alternate)
    elif target == 'history-public':
        mod.history().mkdir(mode=0o755)
    else:
        own = rel.snapshot_hold_path()
        other = own.parent / 'other'
        own.rename(other)
        own.symlink_to(other)
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists()


def test_hold_substitution_after_archive_fails(pending, monkeypatch):
    rel, mod, args, _ = pending
    def substitute():
        own = rel.snapshot_hold_path()
        own.rename(own.with_name('original-preserved'))
        own.write_text(mod.VERSION)
    monkeypatch.setattr(mod, 'quiet', substitute)
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists()
    monkeypatch.setattr(mod, 'quiet', lambda: None)
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists()


def test_evidence_changes_during_resume_fail_closed(pending, monkeypatch):
    rel, mod, args, _ = pending
    original = rel.unlink_pinned_hold
    monkeypatch.setattr(rel, 'unlink_pinned_hold', lambda *_: (_ for _ in ()).throw(OSError('crash')))
    assert rel.main(args) == 1
    monkeypatch.setattr(rel, 'unlink_pinned_hold', original)
    monkeypatch.setattr(mod, 'evidence', lambda _: {'stable': 'changed identities'})
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists()


def test_typed_absence_requires_authenticated_complete_shape(pending, monkeypatch):
    _, mod, _, _ = pending
    # Restore actual quiet rather than fixture's no-op.
    actual = mod
    quiet = mod.quiet.original
    monkeypatch.setattr(actual, 'paginated', lambda endpoint, _: workflow_rows(mod, endpoint.split('/')[1] + '/' + endpoint.split('/')[2]) if endpoint.endswith('/workflows') else [])
    good = {'data': {'repository': {'nameWithOwner': mod.hzlib.PLUGINS, 'release': None}}}
    for code, body in [(1, good), (0, {}), (0, {'data': {'repository': None}}),
                       (0, good | {'errors': ['denied']}), (0, {'data': {'repository': {'nameWithOwner': mod.hzlib.PLUGINS}}}),
                       (0, {'data': {'repository': {'nameWithOwner': mod.hzlib.PLUGINS, 'release': {'tagName': 'app-v0.9.16'}}}})]:
        monkeypatch.setattr(actual.hzlib, 'gh', lambda *_, c=code, b=body: (c, json.dumps(b)))
        with pytest.raises((actual.InvalidRejection, AttributeError)):
            quiet()
    monkeypatch.setattr(actual.hzlib, 'gh', lambda *_: (0, json.dumps(good)))
    quiet()


def test_pagination_exhausts_and_refuses_missing_or_duplicate_entries(pending, monkeypatch):
    _, mod, _, _ = pending
    pages = []
    def api(url):
        page = int(url.rsplit('=', 1)[1])
        pages.append(page)
        return {'total_count': 101, 'jobs': [{'id': i} for i in (range(1, 101) if page == 1 else [101])]}
    monkeypatch.setattr(mod, 'api', api)
    assert len(mod.paginated('jobs', 'jobs')) == 101 and pages == [1, 2]
    monkeypatch.setattr(mod, 'api', lambda _: {'total_count': 2, 'jobs': [{'id': 1}, {'id': 1}]})
    with pytest.raises(mod.InvalidRejection):
        mod.paginated('jobs', 'jobs')
    monkeypatch.setattr(mod, 'api', lambda _: {'total_count': 2, 'jobs': []})
    with pytest.raises(mod.InvalidRejection):
        mod.paginated('jobs', 'jobs')


@pytest.fixture
def remote(pending, monkeypatch):
    """Frozen workflow and typed wire replies, while every network call remains stubbed."""
    from pathlib import Path
    from types import SimpleNamespace
    _rel, mod, _args, _ = pending
    actual_evidence = mod.evidence.original
    run = {'id': 42020635931, 'run_attempt': 1, 'repository': {'full_name': mod.hzlib.PLUGINS},
           'head_repository': {'full_name': mod.hzlib.PLUGINS}, 'head_sha': mod.HEAD, 'head_branch': 'main',
           'path': mod.WORKFLOW, 'event': 'workflow_dispatch', 'status': 'completed', 'conclusion': 'failure', 'workflow_id': 5}
    job = {'id': 12, 'name': 'compose', 'run_id': run['id'], 'head_sha': mod.HEAD, 'status': 'completed', 'conclusion': 'failure',
           'steps': [{'name': 'Build and verify the locally enrolled standard profile', 'status': 'completed', 'conclusion': 'failure'}]}
    release = {'id': 23, 'tag_name': 'v0.9.16', 'draft': False, 'prerelease': False,
               'assets': [{'id': 24, 'name': mod.WHEEL, 'digest': 'sha256:' + mod.WHEEL_SHA, 'browser_download_url': mod.URL}]}
    workflow = Path(__file__).with_name('fixtures').joinpath('rejection-compose.yml').read_bytes()
    replies = {
        f'repos/{mod.hzlib.PLUGINS}/actions/runs/{run["id"]}/attempts/1': run,
        f'repos/{mod.hzlib.PLUGINS}/actions/runs/{run["id"]}': copy.deepcopy(run),
        f'repos/{mod.hzlib.PLUGINS}/actions/workflows/5': {'id': 5, 'path': mod.WORKFLOW},
        f'repos/{mod.hzlib.PLUGINS}/contents/{mod.WORKFLOW}?ref={mod.HEAD}': {'encoding': 'base64', 'content': base64.b64encode(workflow).decode()},
        f'repos/{mod.hzlib.PLUGINS}/compare/{mod.HEAD}...main': {'status': 'ahead', 'merge_base_commit': {'sha': mod.HEAD}},
        f'repos/{mod.hzlib.CORE}/releases/tags/v0.9.16': release,
        f'repos/{mod.hzlib.CORE}/commits/v0.9.16': {'sha': mod.SOURCE}}
    lines = ['##[group]Run test "$REF" = refs/heads/main', 'env:', '  GH_REPO: ' + mod.hzlib.PLUGINS,
             '  CORE_URL: ' + mod.URL, '  VERSION: ' + mod.VERSION, '  REF: refs/heads/main', '##[endgroup]', 'ordinary workload stdout']
    log = '\n'.join('2026-11-05T11:39:15.9556168Z ' + line for line in lines)
    calls = []
    def github(*argv):
        calls.append(argv)
        if argv[0] == 'attestation':
            return 0, json.dumps([{'verificationResult': {'signature': {'certificate': {'issuer': 'verified'}},
                                 'verifiedTimestamps': [{'type': 'transparency-log'}],
                                 'statement': {'predicateType': 'https://slsa.dev/provenance/v1',
                                               'subject': [{'digest': {'sha256': mod.WHEEL_SHA}}]}}}])
        assert argv == ('api', f'repos/{mod.hzlib.PLUGINS}/actions/jobs/12/logs', '--allow-escape-sequences')
        return 0, log
    pypi_url = 'https://files.pythonhosted.org/packages/exact/' + mod.WHEEL
    pypi_metadata = {'info': {'name': 'heinzel', 'version': mod.VERSION}, 'urls': [
        {'filename': mod.WHEEL, 'url': pypi_url, 'size': len(b'exact-wheel'), 'packagetype': 'bdist_wheel', 'digests': {'sha256': mod.WHEEL_SHA}}]}
    class Response:
        def __init__(self, url):
            self.url = url
        def __enter__(self):
            return self
        def __exit__(self, *unused):
            return None
        def read(self, limit):
            return json.dumps(pypi_metadata).encode() if self.url.startswith('https://pypi.org/') else b'exact-wheel'
    # Keep the workflow fingerprint check real; wheel byte digest gets a distinct stub.
    old_digest = mod.digest
    monkeypatch.setattr(mod, 'digest', lambda raw: mod.WHEEL_SHA if raw == b'exact-wheel' else old_digest(raw))
    monkeypatch.setattr(mod, 'api', lambda endpoint: copy.deepcopy(replies[endpoint]))
    monkeypatch.setattr(mod, 'paginated', lambda *_: [copy.deepcopy(job)])
    monkeypatch.setattr(mod, 'quiet', lambda: None)
    monkeypatch.setattr(mod.hzlib, 'gh', github)
    monkeypatch.setattr(mod.urllib.request, 'urlopen', lambda url, **kw: Response(url))
    decision = {'run': run['id'], 'attempt': 1}
    return SimpleNamespace(mod=mod, evidence=actual_evidence, decision=decision,
                           run=run, job=job, release=release, replies=replies, lines=lines, calls=calls, log=log,
                           pypi_metadata=pypi_metadata)


def test_exact_remote_evidence_and_official_provenance_constraints(remote):
    evidence = remote.evidence(remote.decision)
    assert evidence['core_sha256'] == remote.mod.WHEEL_SHA and evidence['head'] == remote.mod.HEAD
    verify = next(argv for argv in remote.calls if argv[0] == 'attestation')
    assert '--deny-self-hosted-runners' in verify
    assert verify[verify.index('--source-digest') + 1] == remote.mod.SOURCE
    assert verify[verify.index('--signer-digest') + 1] == remote.mod.SOURCE
    assert verify[verify.index('--signer-workflow') + 1].endswith('/.github/workflows/publish.yml')


@pytest.mark.parametrize('key,value', [('id', 4), ('run_attempt', 2), ('head_sha', 'a' * 40), ('head_branch', 'untrusted'),
                                     ('path', 'untrusted.yml'), ('event', 'pull_request'), ('status', 'queued'),
                                     ('conclusion', 'success'), ('repository', {'full_name': 'evil/fork'}),
                                     ('head_repository', {'full_name': 'evil/fork'})])
def test_exact_run_mismatches_refused(remote, key, value):
    remote.run[key] = value
    with pytest.raises(remote.mod.InvalidRejection):
        remote.evidence(remote.decision)


@pytest.mark.parametrize('kind', ['rerun', 'workflow', 'workflow-bytes', 'untrusted-main', 'job-head', 'job-run',
                                  'job-success', 'wrong-step', 'wrong-core-source', 'asset-digest', 'asset-url', 'draft-core', 'held-core'])
def test_source_job_and_core_identity_mismatches_refused(remote, kind):
    mod = remote.mod
    if kind == 'rerun':
        remote.replies[f'repos/{mod.hzlib.PLUGINS}/actions/runs/42020635931']['run_attempt'] = 2
    elif kind == 'workflow':
        remote.replies[f'repos/{mod.hzlib.PLUGINS}/actions/workflows/5']['path'] = 'evil.yml'
    elif kind == 'workflow-bytes':
        remote.replies[f'repos/{mod.hzlib.PLUGINS}/contents/{mod.WORKFLOW}?ref={mod.HEAD}']['content'] = base64.b64encode(b'different').decode()
    elif kind == 'untrusted-main':
        remote.replies[f'repos/{mod.hzlib.PLUGINS}/compare/{mod.HEAD}...main']['status'] = 'diverged'
    elif kind == 'job-head':
        remote.job['head_sha'] = 'a' * 40
    elif kind == 'job-run':
        remote.job['run_id'] = 8
    elif kind == 'job-success':
        remote.job['conclusion'] = 'success'
    elif kind == 'wrong-step':
        remote.job['steps'][0]['name'] = 'untrusted text'
    elif kind == 'wrong-core-source':
        remote.replies[f'repos/{mod.hzlib.CORE}/commits/v0.9.16']['sha'] = 'b' * 40
    elif kind == 'asset-digest':
        remote.release['assets'][0]['digest'] = 'sha256:' + 'c' * 64
    elif kind == 'asset-url':
        remote.release['assets'][0]['browser_download_url'] = 'https://example.com/evil.whl'
    elif kind == 'held-core':
        # The closure is pinned to the final 0.9.16 failure; a held pre-release is never that identity.
        remote.release['prerelease'] = True
    else:
        remote.release['draft'] = True
    with pytest.raises(mod.InvalidRejection):
        remote.evidence(remote.decision)


@pytest.mark.parametrize('change', ['VERSION', 'CORE_URL', 'REF', 'duplicate', 'stdout-only', 'incomplete'])
def test_runner_input_frame_is_required(remote, change):
    lines = remote.lines[:]
    if change in ('VERSION', 'CORE_URL', 'REF'):
        lines = [line + '-wrong' if line.startswith('  ' + change + ':') else line for line in lines]
    elif change == 'duplicate':
        lines.insert(-2, '  VERSION: 0.9.16')
    elif change == 'stdout-only':
        lines.insert(2, '##[endgroup]')
    else:
        lines.remove('##[endgroup]')
    log = '\n'.join('2026-11-05T11:39:15.9556168Z ' + line for line in lines)
    with pytest.raises(remote.mod.InvalidRejection):
        remote.mod.log_inputs(log)


@pytest.mark.parametrize('reply', [(1, ''), (0, ''), (0, '[]'), (0, '{}'), (0, 'not JSON')])
def test_unverified_or_unknown_provenance_blocks(remote, monkeypatch, reply):
    monkeypatch.setattr(remote.mod.hzlib, 'gh', lambda *argv: reply if argv[0] == 'attestation' else (0, remote.log))
    with pytest.raises(remote.mod.InvalidRejection):
        remote.evidence(remote.decision)


def test_archive_collision_preserves_existing_history_and_hold(pending, monkeypatch):
    rel, mod, args, _ = pending
    def collide(_):
        mod.history().mkdir(mode=0o700)
        file = mod.history() / (mod.VERSION + '.json')
        file.write_text('collision evidence preserved')
        file.chmod(0o600)
        return {'stable': 'identity'}
    monkeypatch.setattr(mod, 'evidence', collide)
    assert rel.main(args) == 1
    assert (mod.history() / (mod.VERSION + '.json')).read_text() == 'collision evidence preserved'
    assert rel.snapshot_hold_path().exists()


@pytest.mark.parametrize('failure_at', range(1, 13))
def test_each_fsync_boundary_recovers_without_acceptance(pending, monkeypatch, failure_at):
    rel, mod, args, _ = pending
    original = os.fsync
    calls = 0
    def fail_once(fd):
        nonlocal calls
        calls += 1
        if calls == failure_at:
            raise OSError('simulated fsync failure')
        return original(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, 'fsync', fail_once)
        assert rel.main(args) == 1
    assert calls >= failure_at
    assert rel.main(args) == 1
    assert mod.records()[0]['phase'] == 'complete'
    assert rel.state()['release_status'] == 'REJECTED'
    assert 'acceptance_run' not in rel.state()


def test_transaction_has_twelve_fsync_barriers(pending, monkeypatch):
    rel, _, args, _ = pending
    original = os.fsync
    seen = []
    def observe(fd):
        seen.append(fd)
        return original(fd)
    monkeypatch.setattr(os, 'fsync', observe)
    assert rel.main(args) == 1
    assert len(seen) == 12


@pytest.mark.parametrize('operation', ['link', 'replace'])
@pytest.mark.parametrize('after', [False, True])
def test_atomic_publish_interruption_recovers(pending, monkeypatch, operation, after):
    rel, mod, args, _ = pending
    original = getattr(os, operation)
    hit = False
    def fail_once(*a, **kw):
        nonlocal hit
        if not hit:
            hit = True
            if after:
                original(*a, **kw)
            raise OSError('simulated interrupted atomic publication')
        return original(*a, **kw)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, operation, fail_once)
        assert rel.main(args) == 1
    assert hit
    assert rel.main(args) == 1
    assert mod.records()[0]['phase'] == 'complete'


def test_shared_coordinator_lock_blocks_rejection(pending):
    import fcntl
    rel, mod, args, _ = pending
    with rel.paths()[1].with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert rel.main(args) == 1
    assert not mod.records() and rel.snapshot_hold_path().exists()


@pytest.mark.parametrize('operation,index', [('fsync', i) for i in range(1, 13)] + [('link', 1)] + [('replace', i) for i in range(1, 5)])
def test_process_death_at_actual_storage_boundaries(pending, operation, index):
    """os._exit bypasses finally blocks, faithfully retaining link/temp crash windows."""
    import subprocess
    import sys
    rel, mod, args, _ = pending
    script = r'''
import importlib.machinery
import importlib.util
import json
import os
import sys
from pathlib import Path
from steuermaschine_heinzel.native.release_lib import runner as rel
rel.release_rejection.evidence = lambda _: {'stable': 'exact identities', 'verified_provenance': ['verified']}
rel.release_rejection.quiet = lambda: None
original = getattr(os, sys.argv[2])
seen = 0
def die(*args, **kwargs):
    global seen
    result = original(*args, **kwargs)
    seen += 1
    if seen == int(sys.argv[3]):
        os._exit(77)
    return result
setattr(os, sys.argv[2], die)
raise SystemExit(rel.main(json.loads(sys.argv[4])))
'''
    child = subprocess.run([sys.executable, '-c', script, rel.__file__, operation, str(index), json.dumps(args)],
                           capture_output=True, text=True, check=False)
    assert child.returncode == 77, child.stdout + child.stderr
    assert rel.main(args) == 1
    assert mod.records()[0]['phase'] == 'complete'
    assert rel.state()['release_status'] == 'REJECTED' and 'acceptance_run' not in rel.state()


def workflow_rows(mod, repo):
    return [{'id': i, 'path': '.github/workflows/' + name}
            for i, name in enumerate(sorted(mod.PUBLICATION_WORKFLOWS[repo] | mod.ORDINARY_WORKFLOWS[repo]), 1)]


@pytest.mark.parametrize('active', ['ordinary-ci', 'publication', 'unknown-id', 'wrong-path', 'unknown-workflow'])
def test_publication_inventory_allows_only_known_unrelated_work(pending, monkeypatch, active):
    _, mod, _, _ = pending
    quiet = mod.quiet.original
    def inventory(endpoint, key):
        repo = '/'.join(endpoint.split('/')[1:3])
        workflows = workflow_rows(mod, repo)
        if key == 'workflows':
            if active == 'unknown-workflow':
                workflows.append({'id': 999, 'path': '.github/workflows/unreviewed.yml'})
            return workflows
        if 'status=in_progress' not in endpoint:
            return []
        if active == 'publication':
            selected = next(row for row in workflows if row['path'].endswith('/pypi.yml')) if repo == mod.hzlib.CORE else workflows[0]
        elif active == 'unknown-workflow':
            selected = {'id': 999, 'path': '.github/workflows/unreviewed.yml'}
        else:
            selected = next(row for row in workflows if row['path'].endswith('/ci.yml'))
        return [{'id': 888, 'workflow_id': 999 if active == 'unknown-id' else selected['id'],
                 'path': 'wrong.yml' if active == 'wrong-path' else selected['path']}]
    monkeypatch.setattr(mod, 'paginated', inventory)
    monkeypatch.setattr(mod.hzlib, 'gh', lambda *_: (0, json.dumps({'data': {'repository': {'nameWithOwner': mod.hzlib.PLUGINS, 'release': None}}})))
    if active == 'ordinary-ci':
        quiet()
    else:
        with pytest.raises(mod.InvalidRejection):
            quiet()


def test_receipt_replacement_during_remote_read_is_preserved(pending, monkeypatch):
    rel, mod, args, _ = pending
    def change(_):
        rel.write_state(rel.state() | {'new_observed_field': 'preserve'})
        return {'stable': 'identities'}
    monkeypatch.setattr(mod, 'evidence', change)
    assert rel.main(args) == 1
    assert rel.state()['new_observed_field'] == 'preserve'
    assert not mod.records() and rel.snapshot_hold_path().exists()


def test_receipt_replacement_before_unlink_is_preserved(pending, monkeypatch):
    rel, mod, args, _ = pending
    def change():
        rel.write_state(rel.state() | {'new_observed_field': 'preserve'})
    monkeypatch.setattr(mod, 'quiet', change)
    assert rel.main(args) == 1
    assert rel.state()['new_observed_field'] == 'preserve'
    assert rel.snapshot_hold_path().exists()


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
def test_real_github_contents_wrapping_is_accepted(remote, newline):
    encoded = remote.replies[next(key for key in remote.replies if '/contents/' in key)]['content']
    wrapped = newline.join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + newline
    assert remote.mod.digest(remote.mod.workflow_bytes({'encoding': 'base64', 'content': wrapped})) == remote.mod.WORKFLOW_SHA


@pytest.mark.parametrize('bad', [' ', '\t', '!', '\v'])
def test_base64_only_cr_lf_framing_allowed(remote, bad):
    with pytest.raises(ValueError):
        remote.mod.workflow_bytes({'encoding': 'base64', 'content': 'YW' + bad + 'Jj'})


@pytest.mark.parametrize('bad', ['name', 'version', 'digest', 'host', 'size', 'duplicate', 'absent'])
def test_independent_pypi_identity_mismatch_blocks(remote, bad):
    metadata = remote.pypi_metadata
    if bad in ('name', 'version'):
        metadata['info'][bad] = 'wrong'
    elif bad == 'digest':
        metadata['urls'][0]['digests']['sha256'] = 'e' * 64
    elif bad == 'host':
        metadata['urls'][0]['url'] = 'https://example.com/' + remote.mod.WHEEL
    elif bad == 'size':
        metadata['urls'][0]['size'] = 999
    elif bad == 'duplicate':
        metadata['urls'].append(copy.deepcopy(metadata['urls'][0]))
    else:
        metadata['urls'] = []
    with pytest.raises(remote.mod.InvalidRejection):
        remote.evidence(remote.decision)


def test_actual_normal_plugin_markers_preserved_exactly(pending):
    rel, mod, args, _ = pending
    markers = {f'plugin-heinzel-{name}': '2026-11-05T10:30:00Z' for name in ('clock', 'resource', 'tailscale', 'theme')}
    rel.remember(**markers, pypi='2026-11-05T09:30:00Z')
    raw = rel.paths()[1].read_bytes()
    args[args.index('--expected-receipt-sha256') + 1] = mod.digest(raw)
    assert rel.main(args) == 1
    assert base64.b64decode(mod.records()[0]['receipt_base64']) == raw
    assert all(rel.state()[key] == value for key, value in markers.items())


def test_unknown_normal_plugin_marker_shape_blocks(pending):
    rel, mod, args, _ = pending
    rel.remember(**{'plugin-heinzel-clock': {'forged': 'marker'}})
    args[args.index('--expected-receipt-sha256') + 1] = mod.digest(rel.paths()[1].read_bytes())
    assert rel.main(args) == 1
    assert rel.snapshot_hold_path().exists() and not mod.records()


def test_multiline_cache_globs_and_ansi_outside_inputs_allowed(remote):
    continuation = '2026-11-05T11:39:15.9Z   cache-dependency-glob: **/uv.lock\n**/pyproject.toml\n**/requirements*.txt\n'
    colored_command = '2026-11-05T11:39:15.9Z \x1b[36;1muv run --frozen --no-build\x1b[0m\n'
    runner_prelude = remote.log.replace('\n', '\n' + colored_command, 1)
    log = continuation + runner_prelude + '\nordinary unframed cache continuation'
    remote.mod.log_inputs(log)


@pytest.mark.parametrize('spoof', ['unframed-env', 'unframed-end', 'unframed-start', 'stdout-only', 'duplicate-framed-start', 'nested-group'])
def test_multiline_tolerance_cannot_supply_runner_input_evidence(remote, spoof):
    lines = ['2026-11-05T11:39:15.9Z ' + line for line in remote.lines]
    if spoof == 'unframed-env':
        lines[2] = '  GH_REPO: ' + remote.mod.hzlib.PLUGINS
    elif spoof == 'unframed-end':
        lines[-2] = '##[endgroup]'
    elif spoof == 'unframed-start':
        lines[0] = remote.lines[0]
    elif spoof == 'stdout-only':
        lines = ['2026-11-05T11:39:15.9Z ##[group]Run unrelated command',
                 '2026-11-05T11:39:15.9Z ##[endgroup]'] + remote.lines
    elif spoof == 'duplicate-framed-start':
        lines += lines
    else:
        lines.insert(2, '2026-11-05T11:39:15.9Z ##[group]nested workload')
    with pytest.raises(remote.mod.InvalidRejection):
        remote.mod.log_inputs('\n'.join(lines))
