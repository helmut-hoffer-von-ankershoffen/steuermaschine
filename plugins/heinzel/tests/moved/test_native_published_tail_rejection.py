"""Published-tail rejection with private temporary receipts and fake GETs only."""

import base64
import copy
import fcntl
import json
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.release_lib import tail_rejection as tail


@pytest.fixture
def pending_tail(tool, home, monkeypatch):
    rel = tool('hz-release')
    version = '0.9.47'
    data = {'version': version, 'freeze_token': 'a' * 32, 'freeze_lifted_at': '2026-11-16T08:00:00Z',
            'compose': '2026-11-16T08:01:00Z', 'compose_run_id': 101, 'compose_run_attempt': 1,
            'compose_dispatch': {'repo': tail.archive.hzlib.PLUGINS, 'workflow': 'compose.yml',
                                 'event': 'workflow_dispatch', 'ref': 'main', 'sha': 'b' * 40,
                                 'inputs': {'version': version, 'core_wheel':
                                            f'https://github.com/{tail.archive.hzlib.CORE}/releases/download/v{version}/heinzel-{version}-py3-none-any.whl'}},
            'compose_adopted': {'run': 101, 'sha': 'b' * 40, 'created': '2026-11-16T08:01:00Z',
                                'at': '2026-11-16T08:02:00Z'}}
    receipt = rel.tail_receipt(version)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rel, 'RECEIPT_OVERRIDE', receipt)
    rel.write_state(data)
    raw = receipt.read_bytes()
    args = SimpleNamespace(version=version, expected_receipt_sha256=tail.archive.digest(raw),
                           expected_freeze_token='a' * 32, failed_compose_run=101, failed_compose_attempt=2,
                           expected_compose_head='b' * 40, expected_core_source='c' * 40,
                           reason='review: failed published compose is superseded')
    def api(path):
        if '/releases/tags/' in path:
            return {'id': 201, 'tag_name': 'v' + version, 'draft': False, 'prerelease': False,
                    'published_at': '2026-11-16T08:00:00Z'}
        if '/git/ref/tags/' in path:
            return {'ref': 'refs/tags/v' + version, 'object': {'type': 'commit', 'sha': 'c' * 40}}
        if '/actions/runs/' in path:
            attempt = int(path.rsplit('/', 1)[1]) if '/attempts/' in path else 2
            return {'id': 101, 'run_attempt': attempt, 'head_sha': 'b' * 40, 'head_branch': 'main',
                    'path': '.github/workflows/compose.yml', 'event': 'workflow_dispatch',
                    'status': 'completed', 'conclusion': 'failure',
                    'repository': {'full_name': tail.archive.hzlib.PLUGINS},
                    'head_repository': {'full_name': tail.archive.hzlib.PLUGINS}}
        pytest.fail('unexpected GET endpoint')
    monkeypatch.setattr(tail.archive, 'api', api)
    return rel, args, raw, api


def test_archive_keeps_attempt_one_and_two_and_absorption_stays_separate(pending_tail, monkeypatch):
    rel, args, raw, _ = pending_tail
    assert tail.reject(rel, args) == 1
    state = rel.state()
    assert state['finished'] is True and state['accepted'] is False and state['release_status'] == 'REJECTED'
    assert state['compose_run_attempt'] == 1 and 'absorbed_by' not in state
    record = tail.archive.records()[0]
    assert base64.b64decode(record['receipt_base64']) == raw
    assert [r['run_attempt'] for r in record['evidence']['attempts']] == [1, 2]
    assert tail.reject(rel, args) == 1
    monkeypatch.setattr(rel, 'absorber_version', lambda _: '0.9.48')
    assert rel.absorb_release(args.version, 'fixed in 0.9.48 by #850') == 0
    assert rel.state()['absorbed_by'] == '0.9.48' and rel.state()['accepted'] is False
    with pytest.raises(tail.archive.InvalidRejection):
        tail.archive.guard(args.version)


@pytest.mark.parametrize('fault', ['hash', 'token', 'version', 'head', 'source', 'attempt', 'run'])
def test_exact_decision_mismatch_never_writes(pending_tail, fault):
    rel, args, raw, _ = pending_tail
    fields = {'hash': ('expected_receipt_sha256', 'd' * 64), 'token': ('expected_freeze_token', 'd' * 32),
              'version': ('version', '0.9.48'), 'head': ('expected_compose_head', 'd' * 40),
              'source': ('expected_core_source', 'd' * 40), 'attempt': ('failed_compose_attempt', 3),
              'run': ('failed_compose_run', 102)}
    setattr(args, *fields[fault])
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert rel.tail_receipt('0.9.47').read_bytes() == raw and not tail.archive.records()


@pytest.mark.parametrize('field,value', [('accepted', True), ('finished', True), ('acceptance_run', 9),
                                        ('compose_run_attempt', True), ('freeze_lifted_at', 'bad'),
                                        ('compose_dispatch', {}), ('unexpected', 1)])
def test_receipt_not_a_supported_unaccepted_tail(pending_tail, field, value):
    rel, args, _, _ = pending_tail
    rel.write_state(rel.state() | {field: value})
    raw = rel.paths()[1].read_bytes()
    args.expected_receipt_sha256 = tail.archive.digest(raw)
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert rel.paths()[1].read_bytes() == raw


@pytest.mark.parametrize('fault', ['success', 'running', 'rerun', 'repo', 'workflow', 'draft', 'tag'])
def test_remote_changes_fail_closed(pending_tail, monkeypatch, fault):
    rel, args, raw, api = pending_tail
    def changed(path):
        row = copy.deepcopy(api(path))
        if '/actions/runs/' in path:
            if fault == 'success':
                row['conclusion'] = 'success'
            if fault == 'running':
                row['status'] = 'in_progress'
            if fault == 'rerun' and '/attempts/' not in path:
                row['run_attempt'] = 3
            if fault == 'repo':
                row['repository']['full_name'] = 'other/repo'
            if fault == 'workflow':
                row['path'] = '.github/workflows/other.yml'
        if fault == 'draft' and '/releases/tags/' in path:
            row['draft'] = True
        if fault == 'tag' and '/git/ref/' in path:
            row['object']['sha'] = 'd' * 40
        return row
    monkeypatch.setattr(tail.archive, 'api', changed)
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert rel.paths()[1].read_bytes() == raw


@pytest.mark.parametrize('boundary', ['archived', 'pending', 'unlink-authorized', 'final', 'complete'])
@pytest.mark.parametrize('after', [False, True])
def test_interrupted_writes_reconcile_identical_decision(pending_tail, monkeypatch, boundary, after):
    rel, args, raw, _ = pending_tail
    original_persist, original_write = tail.archive.persist, rel.write_state
    hit = False
    def crash(label, action):
        nonlocal hit
        if label == boundary and not hit:
            hit = True
            if after:
                action()
            raise OSError('interrupted test boundary')
        return action()
    def persist(fd, name, record, **kw):
        return crash(record['phase'], lambda: original_persist(fd, name, record, **kw))
    def write(data):
        return crash('final' if data.get('finished') else 'pending', lambda: original_write(data))
    with monkeypatch.context() as scoped:
        scoped.setattr(tail.archive, 'persist', persist)
        scoped.setattr(rel, 'write_state', write)
        with pytest.raises(OSError):
            tail.reject(rel, args)
    assert hit
    assert tail.reject(rel, args) == 1
    assert tail.archive.records()[0]['phase'] == 'complete'
    assert base64.b64decode(tail.archive.records()[0]['receipt_base64']) == raw
    assert rel.state()['accepted'] is False


def test_hold_appearing_during_evidence_is_preserved(pending_tail, monkeypatch):
    rel, args, raw, api = pending_tail
    hold = rel.snapshot_hold_path()
    def changed(path):
        hold.parent.mkdir(parents=True, exist_ok=True)
        hold.write_text(args.version)
        return api(path)
    monkeypatch.setattr(tail.archive, 'api', changed)
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert hold.read_text() == args.version and rel.paths()[1].read_bytes() == raw


def test_changed_decision_replay_never_rewrites_archive(pending_tail):
    rel, args, _, _ = pending_tail
    tail.reject(rel, args)
    record = copy.deepcopy(tail.archive.records()[0])
    args.reason = 'review: a different decision'
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert tail.archive.records()[0] == record


def test_native_runner_routes_rejection_without_dispatch(pending_tail, monkeypatch):
    rel, args, _, _ = pending_tail
    monkeypatch.setattr(rel, 'release_core', lambda *a, **kw: pytest.fail('must not dispatch'))
    argv = [args.version, '--reject-release', '--failed-compose-run', str(args.failed_compose_run),
            '--failed-compose-attempt', str(args.failed_compose_attempt),
            '--expected-receipt-sha256', args.expected_receipt_sha256,
            '--expected-freeze-token', args.expected_freeze_token, '--expected-compose-head', args.expected_compose_head,
            '--expected-core-source', args.expected_core_source, '--reason', args.reason]
    assert rel.main(argv) == 1
    state = json.loads(rel.tail_receipt(args.version).read_text())
    assert state['release_status'] == 'REJECTED'
    assert state['compose_run_attempt'] == 1


def test_receipt_mutation_during_evidence_preserves_new_state(pending_tail, monkeypatch):
    rel, args, _, api = pending_tail
    changed = rel.state() | {'unexpected': 'concurrent update'}
    def mutate(path):
        rel.write_state(changed)
        return api(path)
    monkeypatch.setattr(tail.archive, 'api', mutate)
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert rel.state() == changed and not tail.archive.records()


def test_existing_hold_is_never_removed(pending_tail):
    rel, args, raw, _ = pending_tail
    hold = rel.snapshot_hold_path()
    hold.parent.mkdir(parents=True, exist_ok=True)
    hold.write_text(args.version)
    with pytest.raises(tail.archive.InvalidRejection):
        tail.reject(rel, args)
    assert hold.read_text() == args.version and rel.paths()[1].read_bytes() == raw


def test_existing_tail_lock_prevents_rejection(pending_tail):
    rel, args, raw, _ = pending_tail
    with rel.tail_lock(args.version).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        argv = [args.version, "--reject-release", "--failed-compose-run", "101", "--failed-compose-attempt", "2",
                "--expected-receipt-sha256", args.expected_receipt_sha256,
                "--expected-freeze-token", args.expected_freeze_token, "--expected-compose-head", args.expected_compose_head,
                "--expected-core-source", args.expected_core_source, "--reason", args.reason]
        assert rel.main(argv) == 1
    assert rel.tail_receipt('0.9.47').read_bytes() == raw and not tail.archive.records()
