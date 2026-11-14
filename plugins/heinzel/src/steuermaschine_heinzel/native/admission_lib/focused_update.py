"""Explicit prospective PR identity and ancestry proof; never publishes a ref."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from urllib.parse import quote

from . import focused_admit as a
from steuermaschine.config import child_environment

MODE = 'expected-remote-head/v1'
FIELDS = {'mode', 'pr', 'pr_id', 'repository', 'repository_id', 'branch', 'old', 'new'}
SHA = re.compile(r'[0-9a-f]{40}')


def require(ok, message):
    if not ok:
        raise a.AdmissionError(message)


def validate_option(pr, old, new=None):
    """Only an explicit full old SHA and positive PR can request this mode."""
    require(type(pr) is int and pr > 0, 'expected remote head requires a positive PR')
    require(isinstance(old, str) and SHA.fullmatch(old), 'expected remote head must be a full lowercase SHA')
    require(new is None or old != new, 'expected remote head equals prospective head; use strict mode')


def validate_record(record):
    """Do not accept unknown modes or unbound identity fields in approved data."""
    require(isinstance(record, dict) and set(record) == FIELDS and record.get('mode') == MODE,
            'invalid typed PR update record')
    validate_option(record['pr'], record['old'], record['new'])
    require(isinstance(record['new'], str) and SHA.fullmatch(record['new'])
            and type(record['pr_id']) is int and record['pr_id'] > 0
            and type(record['repository_id']) is int and record['repository_id'] > 0
            and isinstance(record['repository'], str) and isinstance(record['branch'], str),
            'invalid typed PR update identity')


def _git(core, *args, check=True):
    # A caller may be inside a Git hook. Neither its index/object store nor its
    # replacement/graft/config environment may influence the disposable proof.
    env = child_environment(drop_prefixes=('GIT_',))
    env['GIT_NO_REPLACE_OBJECTS'] = '1'
    result = subprocess.run(['git', '--no-replace-objects', '-C', str(core), *args],
                            env=env, capture_output=True, timeout=30)
    if check:
        require(result.returncode == 0, 'PR update Git proof unavailable')
    return result


def _plain_history(core):
    require(_git(core, 'rev-parse', '--is-shallow-repository').stdout.strip() == b'false',
            'PR update requires complete nonshallow history')
    require(not _git(core, 'for-each-ref', '--format=%(refname)', 'refs/replace').stdout.strip(),
            'PR update refuses replacement refs')
    grafts = Path(_git(core, 'rev-parse', '--git-path', 'info/grafts').stdout.decode().strip())
    if not grafts.is_absolute():
        grafts = Path(core) / grafts
    require(not grafts.exists() and not grafts.is_symlink(), 'PR update refuses graft history')


def _official_origin(core, repository):
    # Runtime already resolves the existing trusted repository identity. Read it
    # again with the ancestry proof's clean Git context, including URL rewrites.
    remote = _git(core, 'remote', 'get-url', 'origin').stdout.decode().strip().removesuffix('.git')
    require(remote in (f'https://github.com/{repository}', f'ssh://git@github.com/{repository}',
                       f'git@github.com:{repository}'), 'PR update origin is not the official repository')


def authenticate(rt, plan, pr, old, snapshot, *, expected=None):
    """Recompute an exact binding and real ancestry using only a disposable fetch."""
    new = plan['head']
    validate_option(pr, old, new)
    require(isinstance(new, str) and SHA.fullmatch(new), 'invalid prospective PR head')
    require(snapshot is not None and Path(snapshot).resolve() != rt.core.resolve(),
            'PR update needs its disposable source snapshot')
    _official_origin(rt.core, rt.repo)
    _official_origin(snapshot, rt.repo)
    official = rt.api(f'repos/{rt.repo}')
    repo_id = official.get('id')
    require(type(repo_id) is int and repo_id > 0 and official.get('full_name') == rt.repo
            and official.get('fork') is False, 'official PR update repository identity unavailable')
    pull = rt.api(f'repos/{rt.repo}/pulls/{pr}')
    head, base = pull.get('head'), pull.get('base')
    require(type(pull.get('number')) is int and pull['number'] == pr
            and type(pull.get('id')) is int and pull['id'] > 0 and pull.get('state') == 'open'
            and isinstance(head, dict) and isinstance(base, dict), 'PR update identity changed')
    for side in (head, base):
        repo = side.get('repo')
        require(isinstance(repo, dict) and type(repo.get('id')) is int and repo['id'] == repo_id
                and repo.get('full_name') == rt.repo and repo.get('fork') is False,
                'PR update repository is missing, foreign or forked')
    branch = head.get('ref')
    require(isinstance(branch, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', branch)
            and branch != 'main' and not branch.startswith(('release/', 'bump/'))
            and base.get('ref') == 'main' and head.get('sha') == old,
            'PR update branch or expected remote head changed')
    _git(rt.core, 'check-ref-format', f'refs/heads/{branch}')
    remote = rt.api(f'repos/{rt.repo}/git/ref/heads/{quote(branch, safe="")}')
    require(remote.get('ref') == f'refs/heads/{branch}' and isinstance(remote.get('object'), dict)
            and remote['object'].get('type') == 'commit' and remote['object'].get('sha') == old,
            'official PR branch differs from expected remote head')
    require(_git(rt.core, 'symbolic-ref', '--short', 'HEAD').stdout.decode().strip() == branch
            and _git(rt.core, 'rev-parse', 'HEAD').stdout.decode().strip() == new,
            'local PR update branch or reviewed head changed')
    require(not _git(rt.core, 'status', '--porcelain=v1', '--untracked-files=all').stdout,
            'PR update requires a clean checkout and index')
    for repo in (rt.core, snapshot):
        _plain_history(repo)
    # No caller ref is fetched or changed. Repeated guards refetch the exact
    # authenticated official branch into the existing disposable snapshot only.
    _git(snapshot, 'fetch', '--no-tags', 'origin', f'refs/heads/{branch}:refs/sm-admission/pr-update')
    require(_git(snapshot, 'rev-parse', 'refs/sm-admission/pr-update').stdout.decode().strip() == old,
            'fetched PR update branch changed')
    for commit in (old, new):
        require(_git(snapshot, 'cat-file', '-t', commit).stdout.strip() == b'commit',
                'PR update history lacks exact commit objects')
    require(_git(snapshot, 'merge-base', '--is-ancestor', old, new, check=False).returncode == 0,
            'PR update is not a proved fast-forward')
    record = dict(mode=MODE, pr=pr, pr_id=pull['id'], repository=rt.repo,
                  repository_id=repo_id, branch=branch, old=old, new=new)
    if expected is not None:
        validate_record(expected)
        require(record == expected, 'typed PR update identity changed')
    return record
