"""Evidence boundary for a single reviewed release resulting-main reservation."""

import base64
import hashlib
import os
from pathlib import Path
import pwd
import re
import stat
from urllib.error import HTTPError
from urllib.request import urlopen

from ..admission_lib import focused_admit as a
from ..admission_lib import focused_update as git
from . import release_scope as scope
from ... import environment  # moved: the process context is a typed model
from ..queue_lib import core_facts

MODE = 'release-main-only/v1'
HOOKS = {'pre-commit', 'commit-msg', 'pre-push'}
# The code of the route, bound into every prepared transaction (paths relative to the `native` package).
SOURCES = ('release_admit.py', 'release_admit_lib/release_ops.py', 'release_admit_lib/release_slots.py', 'release_admit_lib/focused_release.py',
           'release_admit_lib/release_scope.py', 'release_admit_lib/release_identity.py', 'admission_lib/focused_admit.py',
           'admission_lib/focused_slots.py', 'admission_lib/focused_update.py', 'admission_lib/focused_resulting.py',
           'admission_lib/focused_replay.py', 'admission_lib/identity.py')
VALIDATOR = 'release_admit_lib/release_scope.py'
HOOK_RESULTS = ('dependency lock matches project metadata', 'gitleaks (credentials in the staged changes)',
                'mypy', 'confidential terms', 'confidential terms in the author and committer',
                'confidential terms in the commit message', 'conventional commit message')
SKIPPED = '(no files to check)Skipped'
# Reviewed Core hook configuration: only file-filtered pre-commit hooks may
# skip on a conflict-free merge. Identity/plugin/commit-message hooks still run.
MERGE_HOOK_CONFIG = '2a29d2fc4ecfb8c02c35f56c12517ef6b5936396'
MERGE_FILE_HOOKS = ('check for added large files', 'check for case conflicts', 'debug statements (python)',
                    'detect private key', 'check for merge conflicts', 'check toml', 'check yaml',
                    'fix end of files', 'trim trailing whitespace', HOOK_RESULTS[0], HOOK_RESULTS[1],
                    'ruff format', 'ruff check', 'mypy', 'confidential terms')
RECOVERY_FIELDS = {'pr', 'pr_id', 'repository_id', 'old_head', 'old_base', 'old_tree', 'failed_run',
                   'failed_attempt', 'pause_sha256', 'prior_reservation', 'main_proof'}


def require(condition, message):
    if not condition:
        raise a.AdmissionError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def account_home():
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def verify_account(rt):
    require(rt.home.resolve() == Path.home().resolve() == account_home(), 'release admission requires the real coordinator HOME')
    require(not environment.state_overrides().any(),
            'coordinator state overrides are not supported for release admission')
    require(rt.github_host == 'github.com', 'release admission requires the canonical GitHub host')


def _review_file(root, record, label):
    relative = record['path']
    require(isinstance(relative, str) and relative and not Path(relative).is_absolute()
            and '..' not in Path(relative).parts, f'unsafe {label} path')
    path = root / relative
    require(path.resolve(strict=True).is_relative_to(root.resolve(strict=True)), f'{label} escapes review root')
    raw = a._stable_file(path, a.MAX_REVIEW_BYTES)
    require(digest(raw) == record['sha256'], f'{label} bytes changed')
    return raw


def _hook_log(root, release):
    log = _review_file(root, release['commit_log'], 'normal commit hook transcript').decode()
    recovery = 'recovery' in release
    skipped = set()
    clean_merge = recovery and all(
        re.findall(r'^' + re.escape(name) + r'\.+([^\r\n]*)\r?$', log, flags=re.MULTILINE) == [SKIPPED]
        for name in (HOOK_RESULTS[1], 'confidential terms'))
    for name in HOOK_RESULTS:
        rows = re.findall(r'^' + re.escape(name) + r'\.+([^\r\n]*)\r?$', log, flags=re.MULTILINE)
        allow_skip = recovery and (name in (HOOK_RESULTS[0], 'mypy') or clean_merge and name in MERGE_FILE_HOOKS)
        require(rows == ['Passed'] or (allow_skip and rows == [SKIPPED]),
                'normal commit hook success transcript is incomplete or ambiguous')
        if rows == [SKIPPED]:
            skipped.add(name)
    if clean_merge:
        for name in MERGE_FILE_HOOKS:
            require(re.findall(r'^' + re.escape(name) + r'\.+([^\r\n]*)\r?$', log, flags=re.MULTILINE) == [SKIPPED],
                    'clean merge requires exact file-filtered no-files hook results')
        require(re.findall(r'^owning plugin lint and unit tests\.+([^\r\n]*)\r?$', log, flags=re.MULTILINE) == ['Passed'],
                'clean merge owning plugin hook must pass')
    terminal = log.strip().splitlines()[-1]
    if recovery:
        match = re.fullmatch(r'\[' + re.escape(branch_name(release['version'])) + r' ([0-9a-f]{7,40})\] .+', terminal)
        require(match and release['head'].startswith(match[1]), 'normal recovery transcript does not bind the exact release commit')
    else:
        require(terminal == 'v' + release['version'], 'normal commit hook transcript has a different release version')
    return skipped


def load_packet(path, review_root):
    raw = a._stable_file(path, a.MAX_PACKET_BYTES)
    packet = a._json_bytes(raw, 'release resulting-main packet')
    require(set(packet) == {'schema', 'mode', 'manifest', 'release', 'review'} and type(packet['schema']) is int
            and packet['schema'] == 1 and packet['mode'] == MODE, 'invalid release resulting-main packet')
    plan, release, review = (packet[key] for key in ('manifest', 'release', 'review'))
    require(isinstance(plan, dict) and plan.get('event') == 'push' and plan.get('ref') == 'refs/heads/main'
            and 'head' not in plan and 'commit' not in plan, 'release reservation requires one headless push-tree plan')
    fields = {'version', 'head', 'base', 'tree', 'validator_sha256', 'hooks', 'hook_runner', 'commit_log'}
    require(isinstance(release, dict) and set(release) in (fields, fields | {'recovery'}), 'invalid typed release source')
    version = release['version']
    require(isinstance(version, str) and scope.VERSION.fullmatch(version), 'invalid release version')
    for key in ('head', 'base', 'tree'):
        a._sha(release[key], f'release {key}')
    require((release['base'], release['tree']) == (plan['base'], plan['tree']), 'release and manifest source disagree')
    a._sha(release['validator_sha256'], 'release semantic validator digest', a.HEX)
    require(isinstance(release['hooks'], dict) and set(release['hooks']) == HOOKS,
            'release requires all three normal hook pins')
    for hook in (*release['hooks'].values(), release['hook_runner']):
        require(isinstance(hook, dict) and set(hook) == {'path', 'sha256'}
                and isinstance(hook['path'], str) and Path(hook['path']).is_absolute(), 'invalid normal hook pin')
        a._sha(hook['sha256'], 'normal hook digest', a.HEX)
    require(isinstance(release['commit_log'], dict) and set(release['commit_log']) == {'path', 'sha256'},
            'independent review must bind the actual normal commit hook transcript')
    if 'recovery' in release:
        recovery = release['recovery']
        require(isinstance(recovery, dict) and set(recovery) in (RECOVERY_FIELDS, RECOVERY_FIELDS | {'previous_packet'})
                and all(type(recovery[key]) is int and recovery[key] > 0 for key in ('pr', 'pr_id', 'repository_id', 'failed_run')),
                'invalid typed paused release recovery')
        for key in ('old_head', 'old_base', 'old_tree'):
            a._sha(recovery[key], key)
        require(recovery['old_head'] != release['head'] and recovery['old_base'] != release['base'],
                'recovery requires a distinct historical head and base')
        a._sha(recovery['pause_sha256'], 'paused release digest', a.HEX)
        for key in ('failed_attempt', 'prior_reservation', 'main_proof', *(['previous_packet'] if 'previous_packet' in recovery else [])):
            record = recovery[key]
            require(isinstance(record, dict) and set(record) == {'path', 'sha256'}, 'invalid recovery evidence reference')
            a._sha(record['sha256'], f'reviewed {key} digest', a.HEX)
    require(isinstance(review, dict) and set(review) == {'path', 'sha256', 'release_sha256', 'manifest_sha256', 'changelog_blob'}
            and review['release_sha256'] == digest(a.canonical(release))
            and review['manifest_sha256'] == digest(a.canonical(plan)), 'release scope or review digest changed')
    _review_file(review_root, review, 'independent release source and plan review')
    _hook_log(review_root, release)
    return packet, raw


def _git(core, *args):
    return git._git(core, *args).stdout


def branch_name(version):
    return 'release/v' + version


def source_proof(rt, packet, review_root):
    verify_account(rt)
    plan, release, review = (packet[key] for key in ('manifest', 'release', 'review'))
    head, base, tree, version = (release[key] for key in ('head', 'base', 'tree', 'version'))
    files = {name: digest(a._stable_file(Path(__file__).resolve().parent.parent / name, a.MAX_REVIEW_BYTES)) for name in SOURCES}
    require(files[VALIDATOR] == release['validator_sha256'], 'reviewed release semantic validator changed')
    _review_file(review_root, review, 'independent release source and plan review')
    skipped = _hook_log(review_root, release)
    git._official_origin(rt.core, rt.repo)
    git._plain_history(rt.core)
    recovery = release.get('recovery')
    parents = [recovery['old_head'], base] if recovery is not None else [base]
    require(_git(rt.core, 'symbolic-ref', '--short', 'HEAD').decode().strip() == branch_name(version)
            and _git(rt.core, 'show', '-s', '--format=%H %T %P', 'HEAD').decode().split() == [head, tree, *parents]
            and not _git(rt.core, '--no-optional-locks', 'status', '--porcelain=v1', '--untracked-files=all'),
            'clean release branch, head, tree or reviewed ordered parents changed')
    if HOOK_RESULTS[1] in skipped or 'confidential terms' in skipped:
        clean_merge_proof(rt.core, recovery['old_head'], base, head, tree)
    for name, pin in release['hooks'].items():
        hook = Path(_git(rt.core, 'rev-parse', '--path-format=absolute', '--git-path', f'hooks/{name}').decode().strip())
        raw_hook = a._stable_file(hook, a.MAX_REVIEW_BYTES)
        require(str(hook) == pin['path'] and os.access(hook, os.X_OK)
                and digest(raw_hook) == pin['sha256']
                and f'PREK="{release["hook_runner"]["path"]}"'.encode() in raw_hook.splitlines(),
                'normal release hook or its pinned interpreter changed')
    runner = Path(release['hook_runner']['path'])
    require(os.access(runner, os.X_OK) and digest(a._stable_file(runner, 32 * 1024 * 1024)) == release['hook_runner']['sha256'],
            'normal release hook interpreter changed')
    if HOOK_RESULTS[0] in skipped:
        require(not _git(rt.core, 'diff', '--name-only', recovery['old_head'], head, '--', 'pyproject.toml', 'uv.lock'),
                'normal lock hook skipped despite staged version metadata changes')
    if 'mypy' in skipped:
        python_paths = ('*.py', '*.pyi')
        inherited = set(_git(rt.core, 'diff', '--name-only', '-z', recovery['old_head'], base, '--', *python_paths).split(b'\0'))
        staged = set(_git(rt.core, 'diff', '--name-only', '-z', recovery['old_head'], head, '--', *python_paths).split(b'\0'))
        release_paths = set(_git(rt.core, 'diff', '--name-only', '-z', base, head, '--', *python_paths).split(b'\0'))
        require(staged <= inherited and not ((staged & release_paths) - {b''}),
                'normal mypy hook skipped despite a new Python change in the corrective commit')
    raw = _git(rt.core, 'diff-tree', '--no-commit-id', '-r', '--raw', '--no-abbrev', '--no-renames', '-z', base, head)
    require(digest(raw) == plan['diff_sha256'], 'reviewed release raw diff changed')

    def read(side, path):
        return _git(rt.core, 'show', f"{release[side]}:{path}")

    old, paths = scope.validate(read, raw, version)
    require(plan['changed'] == dict.fromkeys(paths, 'M'), 'manifest does not cover the complete release diff')
    changelog = (_git(rt.core, 'rev-parse', f'{head}:CHANGELOG.md').decode().strip() if 'CHANGELOG.md' in paths else None)
    require(changelog == review['changelog_blob'], 'exact changelog blob needs separate review binding')
    return dict(previous_version=old, runtime_sha256=files, changelog_blob=changelog), raw


def clean_merge_proof(core, old, base, head, tree):
    """No new merge-resolution bytes may hide behind a file-filtered hook skip."""
    for revision in (old, base, head):
        require(_git(core, 'rev-parse', f'{revision}:.pre-commit-config.yaml').decode().strip() == MERGE_HOOK_CONFIG,
                'clean merge hook configuration is not the reviewed file-filtered configuration')
    # Recompute the ordinary merge, without touching the index or worktree.
    # A conflict returns nonzero; any custom resolution or additional file
    # changes produces a different tree and cannot use this transcript route.
    merge = git._git(core, 'merge-tree', '--write-tree', old, base, check=False)
    require(merge.returncode == 0 and merge.stdout.decode().strip() == tree,
            'file-filtered hook skips require the exact conflict-free parent merge tree')


def remote_identity(rt, packet):
    release = packet['release']
    base, version = release['base'], release['version']
    repo = rt.api(f'repos/{rt.repo}')
    require(repo.get('full_name') == rt.repo and repo.get('fork') is False and repo.get('default_branch') == 'main'
            and type(repo.get('id')) is int and repo['id'] > 0, 'canonical release repository identity changed')
    main = rt.api(f'repos/{rt.repo}/git/ref/heads/main')
    branch = rt.api(f'repos/{rt.repo}/branches/main')
    require(main.get('ref') == 'refs/heads/main' and main['object']['type'] == 'commit'
            and main['object']['sha'] == base and branch.get('name') == 'main'
            and branch.get('protected') is True and branch['commit']['sha'] == base, 'protected main changed')
    refs = _git(rt.core, 'ls-remote', '--refs', 'origin', 'refs/heads/main',
                'refs/heads/' + branch_name(version), 'refs/tags/v' + version).decode().splitlines()
    expected = [base + '\trefs/heads/main']
    if recovery := release.get('recovery'):
        require(repo['id'] == recovery['repository_id'], 'paused release repository changed')
        expected.append(recovery['old_head'] + '\trefs/heads/' + branch_name(version))
    require(sorted(refs) == sorted(expected), 'protected main, release branch or version tag changed')
    return repo['id']


def _run_shape(row, *, identity, head, branch, event, verdict, workflow, repo_id):
    require((row['id'], row['head_sha'], row['head_branch'], row['event'], row['status'], row['conclusion'],
             row['run_attempt'], row['workflow_id'], row['path'], row['repository']['id'], row['head_repository']['id']) ==
            (identity, head, branch, event, 'completed', verdict, 1, workflow, core_facts.at(head).workflow, repo_id, repo_id)
            and type(row['run_attempt']) is int, 'canonical original release or main CI identity changed')


def _official_repo(row, rt, repository_id):
    return (isinstance(row, dict) and type(row.get('id')) is int and row['id'] == repository_id
            and row.get('full_name') == rt.repo and row.get('fork') is False)


def _pypi_absent(version):
    try:
        with urlopen(f'https://pypi.org/pypi/heinzel/{version}/json', timeout=20):
            raise a.AdmissionError('release version already exists on PyPI')
    except HTTPError as error:
        require(error.code == 404, 'PyPI release absence unavailable')


def _paused_receipt(rt, release, recovery):
    raw = a._stable_file(rt.cache / 'release.json', 1024 * 1024)
    require(digest(raw) == recovery['pause_sha256'], 'completed release pause receipt changed')
    pause = a._json_bytes(raw, 'paused release receipt')
    token = pause.get('freeze_token')
    require(set(pause) <= {'version', 'finished', 'freeze_token', 'hold_events'}
            and pause['version'] == release['version'] and pause.get('finished', False) is False
            and isinstance(token, str) and token and isinstance(pause.get('hold_events'), list)
            and len(pause['hold_events']) >= 2, 'release state does not describe an unfinished pre-tag pause')
    request, complete = pause['hold_events'][-2:]
    expected = (release['version'], recovery['pr'], recovery['old_head'], token)
    require(all(tuple(event[key] for key in ('version', 'pr', 'head', 'token')) == expected
                for event in (request, complete))
            and request['action'] == 'pause-request' and request['was_ready'] is True
            and complete['action'] == 'pause' and complete['draft'] is True, 'release pause is incomplete or for different source')


def _recovery_pr(rt, release, recovery):
    root = f'repos/{rt.repo}'
    pr, pr_id, repo_id = (recovery[key] for key in ('pr', 'pr_id', 'repository_id'))
    branch, old, old_base = branch_name(release['version']), recovery['old_head'], recovery['old_base']
    pull = rt.api(f'{root}/pulls/{pr}')
    require((pull['number'], pull['id'], pull['state']) == (pr, pr_id, 'open') and pull['draft'] is True
            and pull['auto_merge'] is None and all(_official_repo(pull[side]['repo'], rt, repo_id) for side in ('head', 'base'))
            and (pull['head']['ref'], pull['head']['sha'], pull['base']['ref'], pull['base']['sha']) == (branch, old, 'main', old_base),
            'exact official old draft release PR changed')
    owner, name = rt.repo.split('/')
    query = '''query($owner:String!,$name:String!,$tag:String!,$version:String!,$branch:String!){
      repository(owner:$owner,name:$name){ref(qualifiedName:$tag){name} release(tagName:$version){tagName}
      pullRequests(first:2,headRefName:$branch,states:[OPEN,CLOSED,MERGED]){
        nodes{number state isDraft headRefOid autoMergeRequest{enabledAt}} pageInfo{hasNextPage}}}}'''
    tag = 'v' + release['version']
    result = rt.run('gh', 'api', 'graphql', '-f', f'query={query}', '-f', f'owner={owner}', '-f', f'name={name}',
                    '-f', f'tag=refs/tags/{tag}', '-f', f'version={tag}', '-f', f'branch={branch}')
    parsed = a._json_bytes(result.stdout, 'official pre-tag recovery identity')
    repo = parsed['data']['repository']
    pulls = repo['pullRequests']
    require(not parsed.get('errors') and repo['ref'] is None and repo['release'] is None
            and pulls['pageInfo']['hasNextPage'] is False
            and pulls['nodes'] == [dict(number=pr, state='OPEN', isDraft=True, headRefOid=old, autoMergeRequest=None)],
            'pre-tag release draft inventory changed')
    _pypi_absent(release['version'])


def prior_recovery_entries(rt, release, recovery, prior, review_root, workflow):
    """A second recovery must prove its earlier admitted recovery, never arbitrary merge ancestry."""
    old, base, tree = (recovery[key] for key in ('old_head', 'old_base', 'old_tree'))
    actual = _git(rt.core, 'show', '-s', '--format=%H %T %P', old).decode().split()
    if 'previous_packet' not in recovery:
        require(actual == [old, tree, base], 'historical release tree or original sole parent changed')
        return {}
    record = recovery['previous_packet']
    raw = _review_file(review_root, record, 'previous recovery packet')
    previous_path = review_root / record['path']
    previous, checked = load_packet(previous_path, previous_path.parent)
    require(checked == raw and prior.get('packet_sha256') == digest(raw), 'previous recovery packet receipt changed')
    info, plan = previous['release'], previous['manifest']
    ancestry = info.get('recovery')
    require(isinstance(ancestry, dict) and 'previous_packet' not in ancestry,
            'second recovery requires an admitted first recovery')
    require((info['version'], info['head'], info['base'], info['tree']) == (release['version'], old, base, tree)
            and prior['manifest_sha256'] == digest(a.canonical(plan)), 'previous recovery source changed')
    require(actual == [old, tree, ancestry['old_head'], base], 'previous recovery ordered parents changed')
    original = _git(rt.core, 'show', '-s', '--format=%H %T %P', ancestry['old_head']).decode().split()
    require(original == [ancestry['old_head'], ancestry['old_tree'], ancestry['old_base']],
            'previous recovery original ancestry changed')
    require((ancestry['pr'], ancestry['pr_id'], ancestry['repository_id'])
            == (recovery['pr'], recovery['pr_id'], recovery['repository_id']), 'previous recovery PR identity changed')
    admission = prior.get('admission_id')
    require(isinstance(admission, str) and re.fullmatch('[0-9a-f]{32}', admission), 'invalid previous admission identity')
    receipt_path = rt.cache / 'focused-admission-receipts' / f'head-{old}-{admission}.json'
    require(a._json_bytes(a._stable_file(receipt_path, a.MAX_REVIEW_BYTES), 'local previous receipt') == prior,
            'previous recovery differs from local successful admission receipt')
    proof = prior.get('release_main', {})
    require(proof.get('mode') == MODE and proof.get('workflow') == workflow
            and all(proof.get(key) == info[key] for key in ('version', 'head', 'base', 'tree'))
            and proof.get('main_manifest_sha256') == prior['manifest_sha256'], 'previous recovery admission proof changed')
    main = a._json_bytes(_review_file(previous_path.parent, ancestry['main_proof'], 'previous protected main'), 'previous main')
    first = a._json_bytes(_review_file(previous_path.parent, ancestry['prior_reservation'], 'original reservation'), 'original reservation')
    require((first['repository'], first['version'], first['head'], first['base'], first['tree'])
            == (rt.repo, release['version'], ancestry['old_head'], ancestry['old_base'], ancestry['old_tree'])
            and first['queue_restored'] is True and first['after'] == first['readback'], 'original reservation changed')
    entries = {'push-tree:' + ancestry['old_tree']: a._sha(first['manifest_sha256'], 'original manifest', a.HEX)}
    require(proof.get('guard', {}).get('recovery') == dict(required_entry=entries, main=main),
            'previous recovery protected proof changed')
    require(main['type'] == 'focused-main/v1' and main['head'] == base and main['attempt'] == 1
            and main['tree'] == _git(rt.core, 'rev-parse', f'{base}^{{tree}}').decode().strip(),
            'previous protected main source changed')
    return entries


def recovery_proof(rt, packet, review_root, workflow):
    """Authenticate the paused failed attempt and separately reviewed repaired main."""
    release = packet['release']
    recovery = release['recovery']
    old, old_base, old_tree = (recovery[key] for key in ('old_head', 'old_base', 'old_tree'))
    base, version = release['base'], release['version']
    repo_id, failed = recovery['repository_id'], recovery['failed_run']
    _paused_receipt(rt, release, recovery)
    _recovery_pr(rt, release, recovery)
    for head in (base, old):
        require(git._git(rt.core, 'merge-base', '--is-ancestor', old_base, head, check=False).returncode == 0,
                'historical release base is not an ancestor of the old candidate and protected main')
    prior = a._json_bytes(_review_file(review_root, recovery['prior_reservation'], 'prior release reservation'), 'prior reservation')
    require((prior['repository'], prior['version'], prior['head'], prior['base'], prior['tree'])
            == (rt.repo, version, old, old_base, old_tree) and prior['queue_restored'] is True
            and prior['after'] == prior['readback'], 'prior successful reservation does not bind the failed release source')
    variables = {'FOCUSED_CI_PLANS', *(f'FOCUSED_CI_PLANS_V2_{i}' for i in range(4))}
    require(set(prior['before']) == set(prior['after']) == variables, 'prior release receipt lacks the five exact values')
    for record in (prior['before'], prior['after']):
        for value in record.values():
            a._sha(value, 'prior release readback digest', a.HEX)
    prior_digest = a._sha(prior['manifest_sha256'], 'prior admitted manifest digest', a.HEX)
    retained = prior_recovery_entries(rt, release, recovery, prior, review_root, workflow)
    root = f'repos/{rt.repo}'
    old_run = a._json_bytes(_review_file(review_root, recovery['failed_attempt'], 'original failed candidate'), 'failed attempt')
    shape = dict(identity=failed, head=old, branch=branch_name(version), event='pull_request', verdict='failure',
                 workflow=workflow, repo_id=repo_id)
    rows = rt.pages(f'{root}/actions/workflows/{workflow}/runs?head_sha={old}', 'workflow_runs')
    require(len(rows) == 1, 'original failed candidate CI is missing or duplicated')
    current = rt.api(f'{root}/actions/runs/{failed}/attempts/1')
    for row in (rows[0], current, old_run):
        _run_shape(row, **shape)
    associations = current['pull_requests']
    require(associations == old_run['pull_requests'] and len(associations) == 1, 'original failed run association changed')
    pr = associations[0]
    require((pr['id'], pr['number'], pr['head']['sha'], pr['head']['ref'], pr['head']['repo']['id'],
             pr['base']['sha'], pr['base']['ref'], pr['base']['repo']['id'])
            == (recovery['pr_id'], recovery['pr'], old, branch_name(version), repo_id, old_base, 'main', repo_id),
            'failed candidate does not bind the exact historical release PR')
    main = a._json_bytes(_review_file(review_root, recovery['main_proof'], 'protected focused-main proof'), 'main proof')
    main_id = main['run']
    require(type(main_id) is int and main_id > 0 and main['type'] == 'focused-main/v1' and type(main['attempt']) is int
            and main['attempt'] == 1 and main['head'] == base
            and main['tree'] == _git(rt.core, 'rev-parse', f'{base}^{{tree}}').decode().strip(),
            'reviewed first-attempt focused-main proof differs from protected main')
    a._sha(main['manifest_sha256'], 'main proof manifest digest', a.HEX)
    main_base = a._sha(main['base'], 'main proof base')
    require(git._git(rt.core, 'merge-base', '--is-ancestor', main_base, base, check=False).returncode == 0,
            'main proof base is outside protected history')
    main_rows = rt.pages(f'{root}/actions/workflows/{workflow}/runs?head_sha={base}&event=push&branch=main', 'workflow_runs')
    require(len(main_rows) == 1, 'protected main CI is missing or duplicated')
    for row in (main_rows[0], rt.api(f'{root}/actions/runs/{main_id}/attempts/1')):
        _run_shape(row, identity=main_id, head=base, branch='main', event='push', verdict='success',
                   workflow=workflow, repo_id=repo_id)
    return dict(required_entry={**retained, 'push-tree:' + old_tree: prior_digest}, main=main)


def validate_manifest(packet, snapshot, source, protected, raw):
    plan, release = packet['manifest'], packet['release']
    base, head, tree = (release[key] for key in ('base', 'head', 'tree'))
    git._plain_history(snapshot)
    require(set(plan['execution']) == set(protected) and 'uv.lock' in protected, 'protected execution inventory changed')
    for path in protected:
        after = _git(snapshot, 'rev-parse', f'{head}:{path}').decode().strip()
        require(after == plan['execution'][path], 'reviewed release execution blob changed')
        if path != 'uv.lock':
            require(_git(snapshot, 'rev-parse', f'{base}:{path}').decode().strip() == after,
                    'release reservation cannot change protected producers')
    key = a.registry_key(plan)
    synthetic = _git(snapshot, '-c', 'user.name=Focused release validation', '-c', 'user.email=review@example.invalid',
                     'commit-tree', '--no-gpg-sign', tree, '-p', base, '-m', 'Prospective release squash').decode().strip()
    require(_git(snapshot, 'show', '-s', '--format=%T %P', synthetic).decode().split() == [tree, base],
            'prospective release squash source changed')
    request = dict(event='push', ref='refs/heads/main', payload={}, base=base, head=synthetic, commit=synthetic,
                   raw_diff=base64.b64encode(raw).decode(), registry={key: plan})
    resolved = a._isolated_resolve(source, snapshot, request)
    require(resolved == {'tree': tree, 'manifest_digest': digest(a.canonical(plan))}, 'canonical release scope changed')


def journal_path(rt, release, suffix):
    return rt.cache / 'focused-release-admission-attempts' / f"v{release['version']}-{release['head']}.{suffix}.json"


def require_no_attempt(rt, release):
    marker = journal_path(rt, release, 'attempt')
    for path in (rt.cache, marker.parent):
        if path.exists() or path.is_symlink():
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
                    'unsafe release admission journal directory')
    require(not marker.exists() and not marker.is_symlink(), 'release admission already attempted for this version and head')


def write_once(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (path.parent, path.parent.parent):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o022,
                'unsafe release admission journal directory')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(a.canonical(value) + b'\n')
        output.flush()
        os.fsync(output.fileno())
    for parent in (path.parent, path.parent.parent):
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
