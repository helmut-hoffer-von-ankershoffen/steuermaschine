"""Release reservation proofs with local Git and fake GitHub/launchd boundaries."""

import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_native_admission_slots import a, env  # noqa: F401
from steuermaschine_heinzel.native.release_admit_lib import release_slots as s
from test_native_admission_resulting import original, prepare_cli

r = s.release
VERSION = '0.9.37'
BRANCH = r.branch_name(VERSION)


def artifact(root, name, value):
    raw = value if isinstance(value, bytes) else a.canonical(value)
    (root / name).write_bytes(raw)
    return dict(path=name, sha256=s.digest(raw))


def hook_log(head, recovery):
    lines = []
    for name in r.HOOK_RESULTS:
        status = r.SKIPPED if recovery and name in (r.HOOK_RESULTS[0], 'mypy') else 'Passed'
        lines.append(f'{name}.....{status}')
    lines.append(f'[{BRANCH} {head[:9]}] chore(release): include selector repair' if recovery else f'v{VERSION}')
    return ('\n'.join(lines) + '\n').encode()


@pytest.fixture(params=[False, True], ids=['fresh', 'recovery'])
def candidate(request, env, monkeypatch):
    rt, _, _ = env
    recovery = request.param
    rt.github_host = 'github.com'
    monkeypatch.delenv('HZ_QUEUE_STATE')
    monkeypatch.setattr(r, 'account_home', lambda: rt.home)

    def git(*args):
        return r._git(rt.core, *args).decode().strip()

    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Release fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    git('remote', 'add', 'origin', f'https://github.com/{rt.repo}.git')
    old, new = '0.9.36', VERSION
    tracked = {
        'pyproject.toml': f'[project]\nname="heinzel"\nversion="{old}"\n[tool.bumpversion]\ncurrent_version="{old}"\n',
        'uv.lock': f'[[package]]\nname="heinzel"\nversion="{old}"\n',
        r.scope.PLUGIN: json.dumps(dict(version=old, name='heinzel')),
        'copier.yml': f'heinzel_version:\n  type: str\n  default: "{old}" # bumped by the release process\n  when: false\n',
        r.scope.ACCEPTANCE: f'FOREGROUND_UI = {{\n    "{old}": ("ui", "serve"),\n}}\n',
        'CHANGELOG.md': '# Changelog\n',
        '.github/workflows/ci.yml': 'unchanged execution source\n',
        '.pre-commit-config.yaml': 'fixture hook configuration\n',
        'tests/test_repaired.py': 'repaired = False\n',
    }
    for name, value in tracked.items():
        path = rt.core / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    git('add', '.')
    git('commit', '-qm', 'base')
    historical_base = git('rev-parse', 'HEAD')
    monkeypatch.setattr(r, 'MERGE_HOOK_CONFIG', git('rev-parse', 'HEAD:.pre-commit-config.yaml'))
    git('checkout', '-qb', BRANCH)
    for name in r.scope.SCOPE:
        data = tracked[name]
        if name == r.scope.ACCEPTANCE:
            data = data.replace('}\n', f'    "{new}": ("ui", "serve"),\n}}\n')
        elif name == 'CHANGELOG.md':
            data += f'\n## {new}\n'
        else:
            data = data.replace(old, new)
        (rt.core / name).write_text(data)
    git('commit', '-qam', 'fresh release')
    historical_head = git('rev-parse', 'HEAD')
    historical_tree = git('rev-parse', 'HEAD^{tree}')
    base = historical_base
    if recovery:
        git('checkout', '-q', 'main')
        (rt.core / 'tests/test_repaired.py').write_text('repaired = True\n')
        git('commit', '-qam', 'protected main selector repair')
        base = git('rev-parse', 'HEAD')
        git('checkout', '-q', BRANCH)
        git('merge', '-q', '--no-ff', 'main', '-m', 'recovery release')
    head, tree = git('rev-parse', 'HEAD'), git('rev-parse', 'HEAD^{tree}')
    raw = r._git(rt.core, 'diff-tree', '--no-commit-id', '-r', '--raw', '--no-abbrev', '--no-renames', '-z', base, head)
    execution = {name: git('rev-parse', f'{head}:{name}') for name in ('uv.lock', '.github/workflows/ci.yml')}
    manifest = dict(event='push', ref='refs/heads/main', base=base, tree=tree, diff_sha256=s.digest(raw),
                    changed=dict.fromkeys(r.scope.SCOPE, 'M'), execution=execution)
    runner = rt.home / 'reviewed-prek'
    runner.write_text('#!/bin/sh\nexit 0\n')
    runner.chmod(0o700)
    hooks = {}
    for name in r.HOOKS:
        hook = rt.core / '.git/hooks' / name
        hook.write_text(f'#!/bin/sh\nPREK="{runner}"\n')
        hook.chmod(0o700)
        hooks[name] = dict(path=str(hook), sha256=s.digest(hook.read_bytes()))
    info = dict(version=VERSION, head=head, base=base, tree=tree, hooks=hooks,
                validator_sha256=s.digest(Path(r.scope.__file__).read_bytes()),
                hook_runner=dict(path=str(runner), sha256=s.digest(runner.read_bytes())),
                commit_log=artifact(rt.home, 'commit.log', hook_log(head, recovery)))
    if recovery:
        info['recovery'] = dict(pr=5, pr_id=9, repository_id=4, old_head=historical_head,
                                old_base=historical_base, old_tree=historical_tree, failed_run=7, pause_sha256='1'*64,
                                **{key: artifact(rt.home, key, {}) for key in ('failed_attempt', 'prior_reservation', 'main_proof')})
    review = dict(**artifact(rt.home, 'review.md', b'independently reviewed exact source\n'),
                  release_sha256=s.digest(a.canonical(info)), manifest_sha256=s.digest(a.canonical(manifest)),
                  changelog_blob=git('rev-parse', 'HEAD:CHANGELOG.md'))
    packet = dict(schema=1, mode=r.MODE, release=info, manifest=manifest, review=review)
    return rt, packet, git, raw


def test_real_git_semantics_and_synthetic_one_parent_resolver(candidate, monkeypatch):
    rt, packet, git, raw = candidate
    info = packet['release']
    path = rt.home / 'packet.json'
    path.write_bytes(a.canonical(packet))
    loaded, _ = r.load_packet(path, rt.home)
    assert loaded == packet
    proof, observed = r.source_proof(rt, packet, rt.home)
    assert proof['previous_version'] == '0.9.36' and observed == raw

    def resolver(source, snapshot, request):
        assert request['event'] == 'push' and request['payload'] == {}
        assert git('show', '-s', '--format=%T %P', request['head']).split() == [info['tree'], info['base']]
        assert request['registry'] == {a.registry_key(packet['manifest']): packet['manifest']}
        return dict(tree=info['tree'], manifest_digest=s.digest(a.canonical(packet['manifest'])))

    monkeypatch.setattr(a, '_isolated_resolve', resolver)
    r.validate_manifest(packet, rt.core, rt.home, tuple(packet['manifest']['execution']), raw)


@pytest.mark.parametrize('defect', ['missing', 'failed', 'duplicate', 'wrong-terminal', 'skip-gitleaks'])
def test_hook_transcripts_fail_closed(candidate, defect):
    rt, packet, _, _ = candidate
    info = packet['release']
    raw = (rt.home / 'commit.log').read_bytes()
    old_row = next(row for row in raw.splitlines(keepends=True) if row.startswith(b'mypy...'))
    if defect == 'missing':
        raw = raw.replace(old_row, b'')
    elif defect == 'failed':
        raw = raw.replace(old_row, b'mypy.....Failed\n')
    elif defect == 'duplicate':
        raw = raw.replace(old_row, old_row + b'mypy.....Error\n')
    elif defect == 'skip-gitleaks':
        raw = raw.replace(b'gitleaks (credentials in the staged changes).....Passed',
                          b'gitleaks (credentials in the staged changes).....(no files to check)Skipped')
    else:
        raw += b'unrelated terminal\n'
    info['commit_log'] = artifact(rt.home, 'commit.log', raw)
    with pytest.raises(a.AdmissionError, match='transcript'):
        r._hook_log(rt.home, info)


@pytest.mark.parametrize('defect', ['lock-dependency', 'project-requirements', 'acceptance-code', 'unexpected-path'])
def test_semantic_scope_rejects_nonrelease_edits(candidate, defect):
    rt, packet, _, raw = candidate
    info = packet['release']
    if defect == 'unexpected-path':
        header = b':100644 100644 ' + b'1' * 40 + b' ' + b'2' * 40 + b' M\0'
        raw += header + b'scripts/focused_plan.py\0'

    def read(side, name):
        data = r._git(rt.core, 'show', f"{info[side]}:{name}")
        changes = {'lock-dependency': ('uv.lock', b'\n[[package]]\nname="foreign"\nversion="1"\n'),
                   'project-requirements': ('pyproject.toml', b'\n[other]\nflag=true\n'),
                   'acceptance-code': (r.scope.ACCEPTANCE, b'print("injected")\n')}
        if side == 'head' and defect in changes and name == changes[defect][0]:
            return data + changes[defect][1]
        return data

    with pytest.raises(r.scope.ReleaseScopeError):
        r.scope.validate(read, raw, VERSION)


@pytest.mark.parametrize('candidate', [True], indirect=True)
def test_recovery_requires_reviewed_ordered_parents(candidate):
    rt, packet, git, _ = candidate
    packet['release']['recovery']['old_head'] = git('rev-parse', 'HEAD^2')
    with pytest.raises(a.AdmissionError, match='ordered parents'):
        r.source_proof(rt, packet, rt.home)


@pytest.fixture
def operation(candidate, env, monkeypatch):
    rt, packet, _, _ = candidate
    _, prior, _ = env
    proof = dict(mode=r.MODE, **{key: packet['release'][key] for key in ('version', 'head', 'base', 'tree')},
                 main_manifest_sha256=s.digest(a.canonical(packet['manifest'])),
                 workflow=99, guard={'recovery': None}, old_entries=1)
    prepared = dict(prior, repository=rt.repo, core_repo=str(rt.core), release_main=proof,
                    after_presence=sorted([s.NAMES[0], s.NAMES[1]]))
    expected = prepared['expected']
    desired = prepared['desired']
    slot = prepared['slot']
    def append(source, legacy, slots, additions):
        assert source == rt.core and legacy == '{}' and slots == ('', '', '', '')
        assert additions == {a.registry_key(packet['manifest']): packet['manifest']}
        return {'result': [0, desired[slot]]}
    monkeypatch.setattr(a, '_isolated_slot_append', append)
    def witness(source, plan, before, after, required):
        assert source == rt.core and plan == packet['manifest'] and before == expected and after == desired and required == {}
        return 1
    monkeypatch.setattr(s.resulting, 'release_registry_proof', witness)
    def guard(runtime, value, root, workflow, planned, *, paused, exceptions=None, full=False):
        assert (runtime, value, root, workflow, planned) == (rt, packet, rt.home, 99, desired)
        assert rt.paused == paused
        s.check_consumers(rt, exceptions or {}, planned, full=full)
        return {'recovery': None}
    monkeypatch.setattr(s, 'release_guard', guard)
    def apply(approval=None):
        return s.apply_release(rt, prepared, packet, rt.core, approval or s.digest(a.canonical(prepared)), rt.home)
    return rt, packet, prepared, apply


@pytest.mark.parametrize('defect', ['none', 'bad-approval', 'late-consumer', 'stale-presence', 'setter', 'readback', 'other', 'restore'])
def test_operating_proof_receipts_and_uncertain_single_attempt(operation, monkeypatch, defect):
    rt, packet, prepared, apply = operation
    info = packet['release']
    marker, observed = (r.journal_path(rt, info, suffix) for suffix in ('attempt', 'observed'))
    original = rt.set_variable
    def write(name, value):
        assert marker.is_file() and rt.paused
        original(name, value)
        if defect == 'setter':
            raise a.AdmissionError('setter uncertain')
    rt.set_variable = write
    rt.defect = defect
    if defect == 'late-consumer':
        rt.rows['queued'] = [dict(id=19, status='queued')]
    if defect == 'stale-presence':
        rt.values[s.NAMES[-1]] = ''
    if defect == 'none':
        original_receipt = a._write_receipt
        def receipt(value, home):
            assert not rt.paused and rt.events[-1] == 'restore'
            return original_receipt(value, home)
        monkeypatch.setattr(a, '_write_receipt', receipt)
        result = json.loads(apply().read_bytes())
        assert result['after'] == result['readback'] and set(result['after']) == set(s.NAMES)
        assert result['queue_restored'] is True and result['presence'] == prepared['after_presence']
    else:
        with pytest.raises(a.AdmissionError):
            apply('bad' if defect == 'bad-approval' else None)
        assert not (rt.cache / 'focused-admission-receipts').exists()
    wrote = defect in {'none', 'setter', 'readback', 'other', 'restore'}
    assert len(rt.writes) == int(wrote) and marker.exists() == wrote
    assert observed.exists() == (wrote and defect != 'setter')
    if wrote:
        with pytest.raises(a.AdmissionError, match='already attempted'):
            apply()
        assert len(rt.writes) == 1
    assert not rt.paused


@pytest.mark.parametrize('option', [['--pr', '5'], ['--reviewed-protected-producer']])
def test_cli_release_mode_excludes_other_admission_authority(option, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail('release option conflict opened the runtime')
    monkeypatch.setattr(s, 'Runtime', unexpected)
    assert s.main(['prepare', 'packet.json', '--core-repo', '.', '--review-root', '.', '--release-main-only', *option]) == 1


def test_release_pins_gh_host_without_leaking_working_repo(monkeypatch):
    calls = []
    rt = object.__new__(s.Runtime)
    rt.github_host, rt.repo = 'github.com', s.hzlib.CORE
    monkeypatch.setenv('GH_HOST', 'other.invalid')
    monkeypatch.setenv('GH_REPO', 'unrelated/repo')
    def run(args, **kwargs):
        calls.append(args)
        assert kwargs['env']['GH_HOST'] == 'github.com' and 'GH_REPO' not in kwargs['env']
        return subprocess.CompletedProcess(args, 0, b'{}', b'')
    monkeypatch.setattr(s.subprocess, 'run', run)
    rt.api(f'repos/{rt.repo}')
    rt.set_variable(s.NAMES[1], '{}')
    assert calls[0][-2:] == ('--hostname', 'github.com')
    assert calls[1][-2:] == ('-R', f'github.com/{rt.repo}')


def recover_api(rt, packet, git, monkeypatch):
    info, workflow = packet['release'], 99
    recovery = info['recovery']
    old, old_base = recovery['old_head'], recovery['old_base']
    repository = dict(id=4, full_name=rt.repo, fork=False)
    old_pr = dict(id=9, number=5, head=dict(sha=old, ref=BRANCH, repo=repository),
                  base=dict(sha=old_base, ref='main', repo=repository))
    pull = dict(old_pr, state='open', draft=True, auto_merge=None)
    def run(identity, head, branch, event, verdict, associations):
        return dict(id=identity, head_sha=head, head_branch=branch, event=event, conclusion=verdict,
                    run_attempt=1, workflow_id=workflow, path='.github/workflows/ci.yml', status='completed',
                    repository=repository, head_repository=repository, pull_requests=associations)
    old_run = run(7, old, BRANCH, 'pull_request', 'failure', [old_pr])
    main_run = run(8, info['base'], 'main', 'push', 'success', [])
    recovery['failed_attempt'] = artifact(rt.home, 'failed_attempt', old_run)
    values = {name: s.digest(b'{}') for name in s.NAMES}
    receipt = dict(repository=rt.repo, version=VERSION, head=old, base=old_base, tree=recovery['old_tree'],
                   manifest_sha256='1'*64, queue_restored=True, before=values, after=values, readback=values)
    recovery['prior_reservation'] = artifact(rt.home, 'prior_reservation', receipt)
    main = dict(type='focused-main/v1', run=8, attempt=1, head=info['base'],
                tree=git('rev-parse', f"{info['base']}^{{tree}}"), base=old_base, manifest_sha256='2'*64)
    recovery['main_proof'] = artifact(rt.home, 'main_proof', main)
    event = dict(version=VERSION, pr=5, head=old, token='fixture-pause')
    pause = dict(version=VERSION, freeze_token='fixture-pause', hold_events=[
        dict(event, action='pause-request', was_ready=True), dict(event, action='pause', draft=True)])
    recovery['pause_sha256'] = artifact(rt.cache, 'release.json', pause)['sha256']
    record = dict(number=5, state='OPEN', isDraft=True, headRefOid=old, autoMergeRequest=None)
    graphql = {'data': {'repository': {'ref': None, 'release': None, 'pullRequests': {
        'nodes': [record], 'pageInfo': {'hasNextPage': False}}}}}
    root = f'repos/{rt.repo}'
    apis = {f'{root}/pulls/5': pull, f'{root}/actions/runs/7/attempts/1': old_run,
            f'{root}/actions/runs/8/attempts/1': main_run,
            f'{root}/actions/workflows/ci.yml': dict(id=workflow, path='.github/workflows/ci.yml', state='active')}
    pages = {f'{root}/actions/workflows/99/runs?head_sha={old}': [copy.deepcopy(old_run)],
             f"{root}/actions/workflows/99/runs?head_sha={info['base']}&event=push&branch=main": [copy.deepcopy(main_run)]}
    rt.api = apis.__getitem__
    rt.pages = lambda path, key: pages.get(path, [])
    def command(*args, **kwargs):
        assert args[:3] == ('gh', 'api', 'graphql') and f'version=v{VERSION}' in args
        return subprocess.CompletedProcess(args, 0, a.canonical(graphql), b'')
    rt.run = command
    monkeypatch.setattr(r, '_pypi_absent', lambda version: version == VERSION)
    return apis, pages, graphql, main, pause


@pytest.mark.parametrize('candidate', [True], indirect=True)
@pytest.mark.parametrize('defect', ['none', 'no-owner', 'held', 'active-ci', 'missing-pause', 'wrong-old-base',
                                    'wrong-old-tree', 'rerun', 'duplicated', 'fork', 'tag', 'main-unverified'])
def test_recovery_guard_authenticates_original_failure_and_retained_old_entry(candidate, monkeypatch, defect):
    rt, packet, git, _ = candidate
    info = packet['release']
    recovery = info['recovery']
    apis, pages, graphql, main, pause = recover_api(rt, packet, git, monkeypatch)
    root = f'repos/{rt.repo}'
    holds = rt.management / 'tools/pr-actions.toml'
    if defect == 'no-owner':
        holds.write_text('')
    elif defect == 'held':
        holds.write_text('"5" = { hold = true }\n')
    elif defect == 'active-ci':
        pages[f"{root}/actions/workflows/99/runs?head_sha={info['head']}"] = [dict(id=20)]
    elif defect == 'missing-pause':
        pause['hold_events'].pop()
        recovery['pause_sha256'] = artifact(rt.cache, 'release.json', pause)['sha256']
    elif defect == 'wrong-old-base':
        apis[f'{root}/pulls/5']['base']['sha'] = info['base']
    elif defect == 'wrong-old-tree':
        recovery['old_tree'] = info['tree']
    elif defect == 'rerun':
        pages[f"{root}/actions/workflows/99/runs?head_sha={recovery['old_head']}"][0]['run_attempt'] = 2
    elif defect == 'duplicated':
        rows = pages[f"{root}/actions/workflows/99/runs?head_sha={recovery['old_head']}"]
        rows.append(rows[0])
    elif defect == 'fork':
        apis[f'{root}/pulls/5']['head']['repo']['fork'] = True
    elif defect == 'tag':
        graphql['data']['repository']['ref'] = dict(name=f'v{VERSION}')
    elif defect == 'main-unverified':
        main['attempt'] = 2
        recovery['main_proof'] = artifact(rt.home, 'main_proof', main)
    monkeypatch.setattr(r, 'remote_identity', lambda rt, packet: 4)
    if defect in {'none', 'no-owner'}:
        proof = s.release_guard(rt, packet, rt.home, 99, {name: '' for name in s.NAMES}, paused=False)
        assert proof['recovery'] == dict(required_entry={'push-tree:' + recovery['old_tree']: '1'*64}, main=main)
    else:
        with pytest.raises(a.AdmissionError):
            s.release_guard(rt, packet, rt.home, 99, {name: '' for name in s.NAMES}, paused=False)


@pytest.mark.parametrize('recovery', [False, True], ids=['fresh', 'recovery'])
def test_release_registry_witness_preserves_exact_old_entries(original, monkeypatch, recovery):
    f = original
    _, _, source, _ = prepare_cli(f, monkeypatch)
    before = {name: f.rt.values.get(name, '') for name in s.NAMES}
    plan = f.packet['manifest']
    after = {**before, s.NAMES[1]: a.canonical({a.registry_key(plan): plan}).decode()}
    historical = next((key, value) for key, value in a.parse_registry(before[s.NAMES[0]]).items()
                      if key.startswith('push-tree:'))
    required = {historical[0]: s.digest(a.canonical(historical[1]))} if recovery else {}
    assert s.resulting.release_registry_proof(source, plan, before, after, required) > 0
    if recovery:
        required[historical[0]] = '0' * 64
        with pytest.raises(a.AdmissionError, match='one-entry append'):
            s.resulting.release_registry_proof(source, plan, before, after, required)
    altered = dict(after, **{s.NAMES[0]: '{}'})
    with pytest.raises(a.AdmissionError, match='one-entry append'):
        s.resulting.release_registry_proof(source, plan, before, altered, {})


@pytest.mark.parametrize('candidate', [True], indirect=True)
@pytest.mark.parametrize('restore_ok', [True, False])
def test_release_real_exclusions_hold_all_locks_and_gate_receipt(operation, restore_ok):
    rt, packet, _, apply = operation
    rt.domain = 'gui/fixture'
    rt.service = 'gui/fixture/queue'
    rt.plist = rt.home / 'queue.plist'
    rt.plist.write_text('reviewed original plist')
    tail = rt.cache / 'release/v0.9.37.lock'
    tail.parent.mkdir()
    tail.touch()
    def launch(*args, **kwargs):
        assert args[0] == 'launchctl'
        action = args[1]
        if action == 'print':
            return subprocess.CompletedProcess(args, 113 if rt.paused else 0,
                b'' if rt.paused else f'path = {rt.plist}\nstate = running'.encode(),
                b'Could not find service' if rt.paused else b'')
        rt.events.append(action)
        rt.paused = action == 'bootout'
        return subprocess.CompletedProcess(args, 0, b'', b'')
    rt.run = launch
    rt.queue_state = lambda **kw: s.Runtime.queue_state(rt, **kw)
    rt.wait_queue_pause = lambda: s.Runtime.wait_queue_pause(rt)
    rt.restore_queue = lambda: s.Runtime.restore_queue(rt)
    rt.exclusions = lambda: s.Runtime.exclusions(rt)
    original = rt.set_variable
    def setter(name, value):
        assert rt.held_version_locks == [tail]
        assert (rt.cache / 'focused-pr-registry.lock').exists()
        original(name, value)
        if not restore_ok:
            rt.plist.write_text('unapproved changed plist')
    rt.set_variable = setter
    if restore_ok:
        assert json.loads(apply().read_bytes())['queue_restored'] is True
        assert rt.events == ['bootout', 'write', 'bootstrap']
    else:
        with pytest.raises(a.AdmissionError, match='restoration refused'):
            apply()
        assert rt.events == ['bootout', 'write']
        assert not (rt.cache / 'focused-admission-receipts').exists()
    assert r.journal_path(rt, packet['release'], 'observed').exists()


def test_release_cli_requires_actual_isolated_startup(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail('unsafe interpreter opened runtime')
    monkeypatch.setattr(s, 'Runtime', unexpected)
    monkeypatch.setattr(s, 'sys', SimpleNamespace(flags=SimpleNamespace(isolated=0), stderr=s.sys.stderr))
    assert s.main(['prepare', 'packet.json', '--core-repo', '.', '--review-root', '.', '--release-main-only']) == 1
    assert 'python3 -I' in capsys.readouterr().err


@pytest.fixture
def second_recovery(candidate, monkeypatch):
    """Real Git shape: original release, first correction, then second correction."""
    rt, previous, git, _ = candidate
    _, _, _, previous_main, _ = recover_api(rt, previous, git, monkeypatch)
    previous['review']['release_sha256'] = s.digest(a.canonical(previous['release']))
    folder = rt.home / 'previous'
    folder.mkdir()
    for name in ('commit.log', 'review.md', 'failed_attempt', 'prior_reservation', 'main_proof'):
        (folder / name).write_bytes((rt.home / name).read_bytes())
    previous_record = artifact(folder, 'packet.json', previous)
    previous_record['path'] = 'previous/packet.json'
    old_info = previous['release']
    original_tree = old_info['recovery']['old_tree']
    values = {name: s.digest(b'{}') for name in s.NAMES}
    receipt = dict(repository=rt.repo, version=VERSION, head=old_info['head'], base=old_info['base'],
                   tree=old_info['tree'], manifest_sha256=s.digest(a.canonical(previous['manifest'])),
                   packet_sha256=previous_record['sha256'], admission_id='a'*32,
                   queue_restored=True, before=values, after=values, readback=values,
                   release_main=dict(mode=r.MODE, workflow=99,
                       **{key: old_info[key] for key in ('version', 'head', 'base', 'tree')},
                       main_manifest_sha256=s.digest(a.canonical(previous['manifest'])),
                       guard=dict(recovery=dict(required_entry={'push-tree:' + original_tree: '1'*64}, main=previous_main))))
    receipts = rt.cache / 'focused-admission-receipts'
    receipts.mkdir()
    local = receipts / f"head-{old_info['head']}-{'a'*32}.json"
    local.write_bytes(a.canonical(receipt))
    git('checkout', '-q', 'main')
    (rt.core / 'tests/test_repaired.py').write_text('repaired = "second correction"\n')
    git('commit', '-qam', 'second protected correction')
    base = git('rev-parse', 'HEAD')
    git('checkout', '-q', BRANCH)
    git('merge', '-q', '--no-ff', 'main', '-m', 'second release recovery')
    packet = copy.deepcopy(previous)
    info = packet['release']
    info.update(head=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'), base=base)
    info['recovery'].update(old_head=old_info['head'], old_base=old_info['base'], old_tree=old_info['tree'])
    packet['manifest'].update(base=base, tree=info['tree'])
    recover_api(rt, packet, git, monkeypatch)
    info['recovery']['prior_reservation'] = artifact(rt.home, 'prior_reservation', receipt)
    info['recovery']['previous_packet'] = previous_record
    return rt, packet, previous, receipt, local


@pytest.mark.parametrize('candidate', [True], indirect=True)
@pytest.mark.parametrize('defect', ['none', 'receipt', 'packet-digest', 'ancestor', 'parents', 'main-proof', 'no-packet'])
def test_second_recovery_requires_exact_prior_admission(second_recovery, defect):
    rt, packet, previous, receipt, local = second_recovery
    recovery = packet['release']['recovery']
    if defect == 'receipt':
        local.write_bytes(a.canonical(dict(receipt, queue_restored=False)))
    elif defect == 'packet-digest':
        receipt['packet_sha256'] = 'f'*64
        recovery['prior_reservation'] = artifact(rt.home, 'prior_reservation', receipt)
    elif defect in ('ancestor', 'parents'):
        key = 'old_base' if defect == 'ancestor' else 'old_head'
        previous['release']['recovery'][key] = packet['release']['head']
        previous['review']['release_sha256'] = s.digest(a.canonical(previous['release']))
        record = artifact(rt.home / 'previous', 'packet.json', previous)
        recovery['previous_packet']['sha256'] = record['sha256']
        receipt['packet_sha256'] = record['sha256']
        recovery['prior_reservation'] = artifact(rt.home, 'prior_reservation', receipt)
        local.write_bytes(a.canonical(receipt))
    elif defect == 'main-proof':
        receipt['release_main']['guard']['recovery']['main']['head'] = 'f'*40
        recovery['prior_reservation'] = artifact(rt.home, 'prior_reservation', receipt)
        local.write_bytes(a.canonical(receipt))
    elif defect == 'no-packet':
        del recovery['previous_packet']
    if defect == 'none':
        result = r.recovery_proof(rt, packet, rt.home, 99)
        assert result['required_entry'] == {
            'push-tree:' + previous['release']['recovery']['old_tree']: '1'*64,
            'push-tree:' + recovery['old_tree']: receipt['manifest_sha256'],
        }
    else:
        with pytest.raises(a.AdmissionError):
            r.recovery_proof(rt, packet, rt.home, 99)
    assert not rt.writes


@pytest.mark.parametrize('candidate', [True], indirect=True)
@pytest.mark.parametrize('defect', ['none', 'identity-skipped', 'plugin-skipped', 'message-skipped',
                                    'missing-file-hook', 'mixed-file-hooks', 'unreviewed-config', 'new-tree'])
def test_clean_merge_hook_transcript_requires_proven_parent_tree(candidate, monkeypatch, defect):
    rt, packet, git, _ = candidate
    info = packet['release']
    lines = [f'{name}.....{r.SKIPPED}' for name in r.MERGE_FILE_HOOKS]
    lines += [f'{name}.....Passed' for name in (
        'confidential terms in the author and committer', 'owning plugin lint and unit tests',
        'confidential terms in the commit message', 'conventional commit message')]
    lines.append(f'[{BRANCH} {info["head"][:9]}] chore(release): ordinary no-conflict merge')
    log = '\n'.join(lines) + '\n'
    if defect in ('identity-skipped', 'plugin-skipped', 'message-skipped'):
        name = {'identity-skipped': 'confidential terms in the author and committer',
                'plugin-skipped': 'owning plugin lint and unit tests',
                'message-skipped': 'confidential terms in the commit message'}[defect]
        log = log.replace(f'{name}.....Passed', f'{name}.....{r.SKIPPED}')
    elif defect == 'missing-file-hook':
        log = log.replace(f'check toml.....{r.SKIPPED}\n', '')
    elif defect == 'mixed-file-hooks':
        log = log.replace(f'check toml.....{r.SKIPPED}', 'check toml.....Passed')
    elif defect == 'unreviewed-config':
        monkeypatch.setattr(r, 'MERGE_HOOK_CONFIG', 'f'*40)
    elif defect == 'new-tree':
        # A real extra resolution file is not an unchanged merge of the parents.
        (rt.core / 'unexpected.txt').write_text('new unaudited bytes\n')
        git('add', 'unexpected.txt')
        git('commit', '--amend', '--no-edit', '-q')
        old_head = info['head']
        info.update(head=git('rev-parse', 'HEAD'), tree=git('rev-parse', 'HEAD^{tree}'))
        log = log.replace(old_head[:9], info['head'][:9])
    info['commit_log'] = artifact(rt.home, 'commit.log', log.encode())
    if defect == 'none':
        proof, _ = r.source_proof(rt, packet, rt.home)
        assert proof['previous_version'] == '0.9.36'
    else:
        with pytest.raises(a.AdmissionError):
            r.source_proof(rt, packet, rt.home)
    assert not rt.writes
