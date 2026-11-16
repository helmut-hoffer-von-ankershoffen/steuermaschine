"""Focused, isolated checks for the explicit reviewed compose replacement."""

import hashlib
import json
import re
from types import SimpleNamespace

import pytest

from steuermaschine_heinzel.native.release_lib import compose_harness

OLD, NEW, CORE = 'a' * 40, 'b' * 40, 'c' * 40
VERSION, RUN = '0.9.48', 123
WHEEL = f'https://github.com/core/repo/releases/download/v{VERSION}/heinzel-{VERSION}-py3-none-any.whl'


class Stop(Exception):
    pass


@pytest.fixture
def world(tmp_path):
    path = tmp_path / 'tail.json'
    data = {'version': VERSION, 'compose_run_id': RUN, 'compose_run_attempt': 1,
            'compose_dispatch': {'repo': 'plugins/repo', 'workflow': 'compose.yml', 'ref': 'main', 'sha': OLD,
                                 'event': 'workflow_dispatch', 'inputs': {'version': VERSION, 'core_wheel': WHEEL}},
            'compose_adopted': {'run': RUN}}
    path.write_text(json.dumps(data))
    state = {'data': data, 'run': {'id': RUN, 'run_attempt': 1, 'path': '.github/workflows/compose.yml',
                                  'event': 'workflow_dispatch', 'head_sha': OLD, 'status': 'completed', 'conclusion': 'failure'},
             'compare': {'status': 'ahead', 'files': [{'status': 'modified', 'filename': 'plugins/heinzel-resource/tests/browser/meters.mjs'}]},
             'main': NEW, 'tag': CORE, 'busy': False, 'app': None, 'dispatched': [], 'dispatch_code': 0,
             'dispatch_response': json.dumps({'workflow_run_id': 456})}
    def ghj(*args, **kwargs):
        if '/git/trees/' in args[1]:
            return {'truncated': False, 'tree': [{'path': 'plugins/heinzel-resource/tests/browser/meters.mjs', 'type': 'blob', 'mode': '100644'}]}
        return state['run'] if '/actions/runs/' in args[1] else state['compare']
    def gh(*args):
        if any('/actions/workflows/' in part for part in args):
            assert state['data']['compose_replacement_guard']['reviewed_head'] == NEW
            state['dispatched'].append(args)
            return state['dispatch_code'], state['dispatch_response']
        return (0, state['main'] if '/commits/main' in args[1] else state['tag'])
    def remember(**fields):
        state['data'] = state['data'] | fields
        path.write_text(json.dumps(state['data']))
    rel = SimpleNamespace(EXACT_VERSION=re.compile(r'\d+\.\d+\.\d+'), Stop=Stop, CORE='core/repo', PLUGINS='plugins/repo',
                          paths=lambda: (None, path), tail_receipt=lambda version: path,
                          release_rejection=SimpleNamespace(private_read=lambda p: p.read_bytes()),
                          state=lambda: state['data'], hzlib=SimpleNamespace(gh=gh, ghj=ghj, say=lambda *args: None),
                          release_producer_ready=lambda tag: True, compose_busy=lambda: state['busy'],
                          published_release=lambda repo, tag: state['app'], remember=remember,
                          now_iso=lambda *args: '2026-11-16T00:00:00Z')
    def call():
        compose_harness.replace(rel, VERSION, RUN, 1, NEW, CORE, hashlib.sha256(path.read_bytes()).hexdigest(), 'review: fixture fix')
    return state, call


def test_preserves_old_binding_and_records_pending_before_one_dispatch(world):
    state, call = world
    call()
    assert len(state['dispatched']) == 1
    assert state['data']['compose_replaced'][0]['failed_run'] == RUN
    assert state['data']['compose_replacement_guard'] == {'failed_run': RUN, 'reviewed_head': NEW}
    assert state['data']['compose_replaced'][0]['receipt_base64']
    assert state['data']['compose_run_id'] == 456 and state['data']['compose_run_attempt'] == 1


@pytest.mark.parametrize('change', ['attempt', 'run_head', 'main', 'tag', 'busy', 'app', 'runtime', 'rename', 'partial'])
def test_refuses_changed_or_unreviewable_evidence_without_dispatch(world, change):
    state, call = world
    if change == 'attempt':
        state['run']['run_attempt'] = 2
    elif change == 'run_head':
        state['run']['head_sha'] = 'd' * 40
    elif change == 'main':
        state['main'] = 'd' * 40
    elif change == 'tag':
        state['tag'] = 'd' * 40
    elif change == 'busy':
        state['busy'] = True
    elif change == 'app':
        state['app'] = {'prerelease': False}
    elif change == 'runtime':
        state['compare']['files'][0]['filename'] = 'plugins/heinzel-resource/src/runtime.py'
    elif change == 'rename':
        state['compare']['files'][0]['status'] = 'renamed'
    elif change == 'partial':
        state['compare']['files'] = state['compare']['files'] * 300
    with pytest.raises(Stop):
        call()
    assert state['dispatched'] == []


def test_uncertain_dispatch_preserves_old_history_and_cannot_be_repeated(world):
    state, call = world
    state['dispatch_code'] = 1
    with pytest.raises(Stop, match='uncertain'):
        call()
    assert len(state['dispatched']) == 1
    assert state['data']['compose_replaced'][0]['failed_run'] == RUN
    assert state['data']['compose_replacement_guard']['reviewed_head'] == NEW
    with pytest.raises(Stop):
        call()
    assert len(state['dispatched']) == 1
