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


