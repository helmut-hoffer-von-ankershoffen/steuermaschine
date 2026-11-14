"""Private, fail-closed closure of the reviewed immutable 0.9.16 compose failure.

This is a failure transaction, never acceptance. Widening the supported identity or
workflow fingerprint requires another source review; no caller-supplied approval.
"""
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from ... import state as statedir  # the plugin's state paths
from ..queue_lib import hzlib

VERSION = '0.9.16'
HEAD = '53218549a225089a7c8fd0c315f05f57b89926cc'
SOURCE = 'b6c96d2847b19e620aec1d56d24d075bbfecc29a'
WHEEL_SHA = 'b45e5a2709f6f230c0e3bde8b9305193744b04b1cb3f8c018675b33d48bc68b9'
WORKFLOW_SHA = 'cbf098ab7278c598699b6b2149dd26f4015f5b5e5ba577dc5671cf6f6af91df2'
WORKFLOW = '.github/workflows/compose.yml'
WHEEL = f'heinzel-{VERSION}-py3-none-any.whl'
URL = f'https://github.com/{hzlib.CORE}/releases/download/v{VERSION}/{WHEEL}'
LIMIT = 4 * 1024 * 1024
PHASES = ('archived', 'unlink-authorized', 'complete')
PUBLICATION_WORKFLOWS = {
    hzlib.CORE: {'release-tag.yml', 'publish.yml', 'pypi.yml', 'acceptance.yml', 'edge.yml', 'release.yml'},
    hzlib.PLUGINS: {'compose.yml', 'release.yml', 'auto-release.yml', 'tools-release.yml'},
}
ORDINARY_WORKFLOWS = {
    hzlib.CORE: {'ci.yml', 'claude-review.yml', 'codeql.yml', 'docs.yml', 'fuzz.yml', 'merge-gate.yml',
                'prepare-release.yml', 'scorecard.yml', 'source-review.yml', 'update-prs.yml'},
    hzlib.PLUGINS: {'ci.yml', 'codeql.yml', 'merge-gate.yml'},
}


class InvalidRejection(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidRejection('duplicate JSON field')
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique)
        if not isinstance(result, dict):
            raise InvalidRejection('expected a JSON object')
        return result
    except (ValueError, UnicodeError) as exc:
        raise InvalidRejection('invalid rejection JSON') from exc


def private_read(path, directory=None):
    """Bounded reads from an owned private file; never follow a symlink/FIFO."""
    if directory is None:
        parent = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(parent)
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise InvalidRejection('unsafe receipt parent directory')
            return private_read(Path(path).name, parent)
        finally:
            os.close(parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > LIMIT):
            raise InvalidRejection('unsafe private receipt/archive permissions or identity')
        raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise InvalidRejection('oversized private receipt/archive')
        return raw


def history():
    return statedir.path("release-rejections")


def open_history(create=False):
    root = history()
    if create:
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
        parent = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise InvalidRejection('unsafe rejection history directory')
    return fd


def check_directory(fd):
    a, b = os.fstat(fd), history().lstat()
    if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino) or not stat.S_ISDIR(b.st_mode):
        raise InvalidRejection('rejection history directory replaced')


def persist(fd, name, data, initial=False):
    """Atomic, durable private history, always through the pinned directory."""
    check_directory(fd)
    if not initial:
        existing = decode(private_read(name, fd))
        validate_record(existing, name)
        if ({key: value for key, value in existing.items() if key != 'phase'}
                != {key: value for key, value in data.items() if key != 'phase'}
                or PHASES.index(existing['phase']) > PHASES.index(data['phase'])):
            raise InvalidRejection('archive collision or changed recorded rejection')
    raw = json.dumps(data, sort_keys=True).encode()
    if len(raw) > LIMIT:
        raise InvalidRejection('rejection archive exceeds limit')
    temporary = '.pending-' + os.urandom(16).hex()
    out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        with os.fdopen(out, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        check_directory(fd)
        if initial:
            os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            os.unlink(temporary, dir_fd=fd)
        else:
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def validate_record(record, filename):
    if (record.get('schema') != 1 or record.get('phase') not in PHASES
            or record.get('decision', {}).get('version') != filename[:-5]):
        raise InvalidRejection('unrecognized rejection archive')
    raw = base64.b64decode(record['receipt_base64'], validate=True)
    if digest(raw) != record['decision']['receipt_sha256']:
        raise InvalidRejection('original receipt archive digest mismatch')
    original = decode(raw)
    if original.get('version') != record['decision']['version'] or original.get('freeze_token') != record['decision']['token']:
        raise InvalidRejection('archive original receipt identity mismatch')
    return original


def reconcile_archive_alias(fd, name):
    """Finish only an interrupted exclusive hard-link publication, never a foreign link."""
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if info.st_nlink == 1:
        return
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_nlink != 2):
        raise InvalidRejection('unsafe archive hard-link identity')
    aliases = []
    for candidate in os.listdir(fd):
        other = os.stat(candidate, dir_fd=fd, follow_symlinks=False)
        if candidate != name and (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
            aliases.append(candidate)
    if len(aliases) != 1 or not re.fullmatch(r'\.pending-[a-f0-9]{32}', aliases[0]):
        raise InvalidRejection('archive has an unknown hard-link alias')
    check_directory(fd)
    os.unlink(aliases[0], dir_fd=fd)
    os.fsync(fd)


def records():
    try:
        fd = open_history()
    except FileNotFoundError:
        return []
    try:
        result = []
        for name in os.listdir(fd):
            # Atomic-write temporaries contain no decision yet; a receipt/hold is
            # still untouched. All other unknown entries fail closed.
            if re.fullmatch(r'\.pending-[a-f0-9]{32}', name):
                continue
            if not re.fullmatch(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.json', name):
                raise InvalidRejection('unknown rejection history entry')
            check_directory(fd)
            reconcile_archive_alias(fd, name)
            record = decode(private_read(name, fd))
            validate_record(record, name)
            result.append(record)
        return result
    finally:
        os.close(fd)


def guard(version):
    for record in records():
        if record['phase'] != 'complete':
            raise InvalidRejection('pending release rejection must be reconciled with the identical rejection command')
        if record['decision']['version'] == version:
            raise InvalidRejection('rejected release version cannot be resumed or dispatched')


def api(endpoint):
    code, output = hzlib.gh('api', endpoint)
    if code or len(output) > LIMIT:
        raise InvalidRejection('GitHub evidence unavailable or oversized')
    return decode(output)


def paginated(endpoint, key):
    """Require stable total_count and unique IDs, including every page."""
    result, seen, total = [], set(), None
    for page in range(1, 102):
        data = api(f'{endpoint}{"&" if "?" in endpoint else "?"}per_page=100&page={page}')
        count, rows = data.get('total_count'), data.get(key)
        if type(count) is not int or count < 0 or count > 10000 or not isinstance(rows, list):
            raise InvalidRejection('unknown or incomplete paginated evidence')
        if total is None:
            total = count
        if total != count or len(rows) > 100:
            raise InvalidRejection('publication inventory changed during pagination')
        for row in rows:
            identity = row.get('id') if isinstance(row, dict) else None
            if type(identity) is not int or identity <= 0 or identity in seen:
                raise InvalidRejection('invalid or repeated inventory identity')
            seen.add(identity)
        result.extend(rows)
        if len(result) == total:
            return result
        if not rows or len(result) > total:
            break
    raise InvalidRejection('publication inventory pagination incomplete')


def quiet():
    # Enumerate every workflow and every active page. Only the independently
    # identified publication/acceptance workflows block; ordinary CI may run.
    # An unknown active workflow or ID/path mismatch requires source review.
    for repo in (hzlib.CORE, hzlib.PLUGINS):
        workflows = paginated(f'repos/{repo}/actions/workflows', 'workflows')
        by_id = {}
        paths = set()
        for workflow in workflows:
            path = workflow.get('path')
            if not isinstance(path, str) or path in paths:
                raise InvalidRejection('unknown or ambiguous workflow inventory identity')
            paths.add(path)
            by_id[workflow['id']] = path
        relevant = {'.github/workflows/' + name for name in PUBLICATION_WORKFLOWS[repo]}
        ordinary = {'.github/workflows/' + name for name in ORDINARY_WORKFLOWS[repo]}
        if not relevant <= paths:
            raise InvalidRejection('publication workflow inventory is incomplete')
        for status in ('queued', 'in_progress', 'waiting', 'pending', 'requested'):
            for run in paginated(f'repos/{repo}/actions/runs?status={status}', 'workflow_runs'):
                path = by_id.get(run.get('workflow_id'))
                if path is None or run.get('path') != path or path not in relevant | ordinary:
                    raise InvalidRejection('unknown active workflow identity prevents rejection')
                if path in relevant:
                    raise InvalidRejection('active publication or acceptance work prevents release rejection')
    owner, name = hzlib.PLUGINS.split('/')
    query = 'query($owner:String!,$name:String!,$tag:String!){repository(owner:$owner,name:$name){nameWithOwner release(tagName:$tag){tagName}}}'
    code, output = hzlib.gh('api', 'graphql', '-f', f'query={query}', '-f', f'owner={owner}', '-f', f'name={name}', '-f', f'tag=app-v{VERSION}')
    result = decode(output)
    repo = result.get('data', {}).get('repository')
    if (code or result.get('errors') or not isinstance(repo, dict)
            or repo.get('nameWithOwner') != hzlib.PLUGINS or 'release' not in repo or repo['release'] is not None):
        raise InvalidRejection('typed API evidence does not prove combined app absent')


def log_inputs(log):
    """Only the runner-generated prelude of the exact frozen compose step counts.

    Workload stdout after endgroup cannot impersonate input evidence.
    """
    if len(log) > 32 * 1024 * 1024:
        raise InvalidRejection('compose log too large')
    lines = []
    for line in log.splitlines():
        match = re.fullmatch(r'\ufeff?\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z (.*)', line)
        # setup-uv cache globs contain legitimate multiline continuations.
        # Preserve their positions so an unframed line inside the selected
        # runner prelude still fails closed. Never promote it into evidence.
        lines.append(match[1] if match else None)
    start = '##[group]Run test "$REF" = refs/heads/main'
    if lines.count(start) != 1:
        raise InvalidRejection('missing or ambiguous trusted runner input prelude')
    offset = lines.index(start)
    try:
        end = lines.index('##[endgroup]', offset)
    except ValueError as exc:
        raise InvalidRejection('incomplete trusted runner input prelude') from exc
    group = lines[offset:end]
    if any(line is None for line in group) or any(line.startswith('##[group]') for line in group[1:]):
        raise InvalidRejection('unframed or nested content in trusted runner prelude')
    if group.count('env:') != 1:
        raise InvalidRejection('missing runner environment')
    env = group[group.index('env:') + 1:]
    for key, expected in {'GH_REPO': hzlib.PLUGINS, 'CORE_URL': URL, 'VERSION': VERSION, 'REF': 'refs/heads/main'}.items():
        entries = [line for line in env if line.startswith(f'  {key}:')]
        if entries != [f'  {key}: {expected}']:
            raise InvalidRejection('compose runner inputs differ from published release')


def workflow_bytes(source):
    encoded = source.get('content')
    if source.get('encoding') != 'base64' or not isinstance(encoded, str):
        raise InvalidRejection('unknown workflow contents encoding')
    # GitHub wraps content with LF. Accept CR/LF framing only, never other
    # whitespace or ignored punctuation in independently fingerprinted bytes.
    return base64.b64decode(encoded.replace('\r', '').replace('\n', ''), validate=True)


def pypi_identity(core_bytes):
    with urllib.request.urlopen(f'https://pypi.org/pypi/heinzel/{VERSION}/json', timeout=30) as response:
        if urllib.parse.urlsplit(response.url).netloc != 'pypi.org' or not response.url.startswith('https://'):
            raise InvalidRejection('unsafe PyPI metadata redirect')
        raw_metadata = response.read(LIMIT + 1)
        if len(raw_metadata) > LIMIT:
            raise InvalidRejection('PyPI metadata exceeds limit')
        metadata = decode(raw_metadata)
    if metadata.get('info', {}).get('name') != 'heinzel' or metadata.get('info', {}).get('version') != VERSION:
        raise InvalidRejection('PyPI package/version identity mismatch')
    files = metadata.get('urls')
    if not isinstance(files, list):
        raise InvalidRejection('unknown PyPI file inventory')
    wheels = [item for item in files if isinstance(item, dict) and item.get('filename') == WHEEL]
    if len(wheels) != 1:
        raise InvalidRejection('PyPI wheel missing or ambiguous')
    wheel = wheels[0]
    url = wheel.get('url')
    parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if (parsed is None or parsed.scheme != 'https' or parsed.netloc != 'files.pythonhosted.org'
            or parsed.query or parsed.fragment or not parsed.path.endswith('/' + WHEEL)
            or wheel.get('packagetype') != 'bdist_wheel' or wheel.get('digests', {}).get('sha256') != WHEEL_SHA
            or type(wheel.get('size')) is not int or wheel['size'] <= 0 or wheel['size'] > 64 * 1024 * 1024):
        raise InvalidRejection('PyPI wheel identity/digest/URL mismatch')
    with urllib.request.urlopen(url, timeout=30) as response:
        final = urllib.parse.urlsplit(response.url)
        if final.scheme != 'https' or final.netloc != 'files.pythonhosted.org':
            raise InvalidRejection('unsafe PyPI wheel redirect')
        raw = response.read(64 * 1024 * 1024 + 1)
    if len(raw) != wheel['size'] or raw != core_bytes or digest(raw) != WHEEL_SHA:
        raise InvalidRejection('PyPI wheel bytes differ from official GitHub wheel')
    return {'pypi_url': url, 'pypi_sha256': WHEEL_SHA, 'pypi_size': len(raw)}


def evidence(decision):
    run_id, attempt = decision['run'], decision['attempt']
    run = api(f'repos/{hzlib.PLUGINS}/actions/runs/{run_id}/attempts/{attempt}')
    if (run.get('id') != run_id or run.get('run_attempt') != attempt
            or run.get('repository', {}).get('full_name') != hzlib.PLUGINS
            or run.get('head_repository', {}).get('full_name') != hzlib.PLUGINS
            or run.get('head_sha') != HEAD or run.get('head_branch') != 'main'
            or run.get('path') != WORKFLOW or run.get('event') != 'workflow_dispatch'
            or run.get('status') != 'completed' or run.get('conclusion') != 'failure'):
        raise InvalidRejection('failed compose run identity mismatch')
    latest = api(f'repos/{hzlib.PLUGINS}/actions/runs/{run_id}')
    if latest.get('run_attempt') != attempt or latest.get('head_sha') != HEAD:
        raise InvalidRejection('compose run was rerun or changed')
    workflow = api(f'repos/{hzlib.PLUGINS}/actions/workflows/{run["workflow_id"]}')
    if workflow.get('id') != run['workflow_id'] or workflow.get('path') != WORKFLOW:
        raise InvalidRejection('compose workflow identity mismatch')
    source = api(f'repos/{hzlib.PLUGINS}/contents/{WORKFLOW}?ref={HEAD}')
    content = workflow_bytes(source)
    if digest(content) != WORKFLOW_SHA:
        raise InvalidRejection('compose workflow lacks reviewed fingerprint')
    comparison = api(f'repos/{hzlib.PLUGINS}/compare/{HEAD}...main')
    if comparison.get('status') not in ('ahead', 'identical') or comparison.get('merge_base_commit', {}).get('sha') != HEAD:
        raise InvalidRejection('compose source is not on trusted main')
    jobs = paginated(f'repos/{hzlib.PLUGINS}/actions/runs/{run_id}/attempts/{attempt}/jobs', 'jobs')
    compose = [job for job in jobs if job.get('name') == 'compose']
    if len(compose) != 1:
        raise InvalidRejection('failed compose job missing or ambiguous')
    job = compose[0]
    if job.get('run_id') != run_id or job.get('head_sha') != HEAD or job.get('conclusion') != 'failure' or job.get('status') != 'completed':
        raise InvalidRejection('compose job identity mismatch')
    steps = [step for step in job.get('steps', []) if step.get('name') == 'Build and verify the locally enrolled standard profile']
    if len(steps) != 1 or steps[0].get('conclusion') != 'failure' or steps[0].get('status') != 'completed':
        raise InvalidRejection('compose failure is not the reviewed build step')
    code, log = hzlib.gh('api', f'repos/{hzlib.PLUGINS}/actions/jobs/{job["id"]}/logs', '--allow-escape-sequences')
    if code:
        raise InvalidRejection('exact compose job log unavailable')
    log_inputs(log)
    release = api(f'repos/{hzlib.CORE}/releases/tags/v{VERSION}')
    if release.get('tag_name') != f'v{VERSION}' or release.get('draft') is not False or release.get('prerelease') is not False:
        raise InvalidRejection('Core release is not published final')
    assets = [asset for asset in release.get('assets', []) if asset.get('name') == WHEEL]
    if len(assets) != 1 or assets[0].get('digest') != f'sha256:{WHEEL_SHA}' or assets[0].get('browser_download_url') != URL:
        raise InvalidRejection('published Core wheel identity mismatch')
    if api(f'repos/{hzlib.CORE}/commits/v{VERSION}').get('sha') != SOURCE:
        raise InvalidRejection('Core source tag changed')
    with tempfile.TemporaryDirectory(prefix='sm-rejection-provenance-') as temp:
        wheel = Path(temp) / WHEEL
        with urllib.request.urlopen(URL, timeout=30) as response:
            if not response.url.startswith('https://'):
                raise InvalidRejection('unsafe Core wheel redirect')
            raw = response.read(64 * 1024 * 1024 + 1)
        if len(raw) > 64 * 1024 * 1024 or digest(raw) != WHEEL_SHA:
            raise InvalidRejection('Core wheel bytes differ from published digest')
        registry = pypi_identity(raw)
        wheel.write_bytes(raw)
        code, proof = hzlib.gh('attestation', 'verify', str(wheel), '--repo', hzlib.CORE,
                             '--signer-workflow', f'{hzlib.CORE}/.github/workflows/publish.yml',
                             '--signer-digest', SOURCE,
                             '--source-digest', SOURCE, '--deny-self-hosted-runners', '--format', 'json')
        if code or not proof.strip() or len(proof) > LIMIT // 2:
            raise InvalidRejection('official Core wheel provenance could not be verified')
        try:
            parsed = json.loads(proof)
            if not isinstance(parsed, list) or not parsed:
                raise ValueError()
            for verified in parsed:
                result = verified['verificationResult']
                statement = result['statement']
                if (statement['predicateType'] != 'https://slsa.dev/provenance/v1'
                        or not isinstance(result['signature']['certificate'], dict)
                        or not result['signature']['certificate']
                        or not isinstance(result['verifiedTimestamps'], list) or not result['verifiedTimestamps']
                        or not any(subject.get('digest', {}).get('sha256') == WHEEL_SHA for subject in statement['subject'])):
                    raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise InvalidRejection('unknown verified provenance format') from exc
    quiet()
    return {'core_source': SOURCE, 'core_sha256': WHEEL_SHA, 'core_url': URL,
            'release_id': release['id'], 'asset_id': assets[0]['id'],
            'run': run_id, 'attempt': attempt, 'head': HEAD, 'workflow_id': run['workflow_id'],
            'workflow_sha256': WORKFLOW_SHA, 'job_id': job['id'], 'log_sha256': digest(log.encode()),
            'verified_provenance': parsed, **registry}


def reject(rel, args):
    decision = {'version': args.version, 'receipt_sha256': args.expected_receipt_sha256,
                'token': args.expected_freeze_token, 'run': args.failed_compose_run,
                'attempt': args.failed_compose_attempt, 'head': args.expected_compose_head,
                'source': args.expected_core_source, 'reason': args.reason}
    if (args.version != VERSION or decision['head'] != HEAD or decision['source'] != SOURCE
            or type(decision['run']) is not int or decision['run'] <= 0
            or type(decision['attempt']) is not int or decision['attempt'] <= 0
            or not re.fullmatch('[a-f0-9]{64}', decision['receipt_sha256'] or '')
            or not re.fullmatch('[a-f0-9]{32}', decision['token'] or '')
            or not isinstance(decision['reason'], str) or not decision['reason'].strip()
            or len(decision['reason']) > 4000):
        raise InvalidRejection('rejection requires exact reviewed version/source/head and complete identities')
    all_records = records()
    matching = [record for record in all_records if record['decision']['version'] == VERSION]
    if any(record['phase'] != 'complete' and record not in matching for record in all_records):
        raise InvalidRejection('another rejection is pending')
    receipt_info = rel.paths()[1].lstat()
    receipt_identity = (receipt_info.st_dev, receipt_info.st_ino)
    raw = private_read(rel.paths()[1])
    data = decode(raw)
    if matching:
        record = matching[0]
        if record['decision'] != decision:
            raise InvalidRejection('rejection replay identity changed')
        original = validate_record(record, VERSION + '.json')
        allowed = [original, original | {'rejection': decision, 'release_status': 'REJECTING', 'finished': False},
                   original | {'rejection': decision, 'release_status': 'REJECTED', 'finished': True}]
        if data not in allowed:
            raise InvalidRejection('receipt changed since rejection archive')
        if data == original and digest(raw) != decision['receipt_sha256']:
            raise InvalidRejection('raw receipt changed since rejection archive')
    else:
        if (data.get('version') != VERSION or data.get('freeze_token') != decision['token']
                or data.get('finished') or 'acceptance_identity' in data or 'acceptance_run' in data
                or set(data) - {'version', 'freeze_token', 'finished', 'hold_events', 'pypi', 'pypi_run_id', 'pypi_run_attempt', 'compose', 'compose_run_id', 'compose_run_attempt',
                                  'plugin-heinzel-clock', 'plugin-heinzel-resource', 'plugin-heinzel-tailscale', 'plugin-heinzel-theme'}
                or 'release_status' in data or 'rejection' in data or not isinstance(data.get('compose'), str)
                or (data.get('compose_run_id') is not None and data['compose_run_id'] != decision['run'])
                or (data.get('compose_run_attempt') is not None and data['compose_run_attempt'] != decision['attempt'])
                or digest(raw) != decision['receipt_sha256']):
            raise InvalidRejection('rejection requires exact unfinished compose receipt and raw digest')
        rel.hold_events(data)
        for key in ('compose', 'pypi', 'plugin-heinzel-clock', 'plugin-heinzel-resource', 'plugin-heinzel-tailscale', 'plugin-heinzel-theme'):
            if key in data:
                value = data[key]
                if not isinstance(value, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', value):
                    raise InvalidRejection('unrecognized normal dispatch marker shape')
                datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ')
        original = data
        record = None
    directory = None
    try:
        exists = rel.owned_hold_exists(VERSION)
        if exists:
            directory, identity = rel.pin_owned_hold(VERSION)
            info = os.stat(decision['token'], dir_fd=directory, follow_symlinks=False)
            if info.st_uid != os.getuid() or info.st_mode & 0o022 or info.st_nlink != 1:
                raise InvalidRejection('unsafe owned release hold')
            if record and ([os.fstat(directory).st_dev, os.fstat(directory).st_ino] != record['hold_directory_identity']
                           or list(identity) != record['hold_identity']):
                raise InvalidRejection('owned hold inode changed since rejection archive')
        elif not record or record['phase'] == 'archived':
            raise InvalidRejection('owned hold missing before durable unlink authorization')
        if record and record['phase'] == 'complete':
            if exists or data != allowed[2]:
                raise InvalidRejection('completed rejection state changed')
            fd = open_history()
            try:
                persist(fd, VERSION + '.json', record)
            finally:
                os.close(fd)
            print(f'{VERSION} rejected; published Core retained; combined app not accepted')
            return 1
        observed = evidence(decision)
        current = rel.paths()[1].lstat()
        if (current.st_dev, current.st_ino) != receipt_identity or private_read(rel.paths()[1]) != raw:
            raise InvalidRejection('receipt file changed while remote evidence was collected')
        fd = open_history(create=True)
        try:
            if record is None:
                record = {'schema': 1, 'phase': 'archived', 'decision': decision,
                          'receipt_base64': base64.b64encode(raw).decode(), 'evidence': observed,
                          'hold_identity': list(identity),
                          'hold_directory_identity': [os.fstat(directory).st_dev, os.fstat(directory).st_ino]}
                persist(fd, VERSION + '.json', record, initial=True)
            else:
                stable = lambda e: {key: value for key, value in e.items() if key != 'verified_provenance'}
                if stable(record['evidence']) != stable(observed):
                    raise InvalidRejection('published or failed-run evidence changed since archive')
                # Repeat the durability barrier after ambiguous previous fsync.
                persist(fd, VERSION + '.json', record)
            rel.write_state(original | {'rejection': decision, 'release_status': 'REJECTING', 'finished': False})
            pending_raw = private_read(rel.paths()[1])
            pending_info = rel.paths()[1].lstat()
            quiet()  # Fresh typed absence and complete inventory immediately before hold removal.
            record = record | {'phase': 'unlink-authorized'}
            persist(fd, VERSION + '.json', record)
            current = rel.paths()[1].lstat()
            if ((current.st_dev, current.st_ino) != (pending_info.st_dev, pending_info.st_ino)
                    or private_read(rel.paths()[1]) != pending_raw):
                raise InvalidRejection('receipt changed before hold removal')
            if exists:
                rel.unlink_pinned_hold(directory, tuple(record['hold_identity']))
            else:
                # An unlink may have succeeded before its directory fsync failed.
                hold_dir = os.open(hzlib.release_holds(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    info = os.fstat(hold_dir)
                    if [info.st_dev, info.st_ino] != record['hold_directory_identity']:
                        raise InvalidRejection('hold directory changed after interrupted unlink')
                    os.fsync(hold_dir)
                finally:
                    os.close(hold_dir)
            rel.write_state(original | {'rejection': decision, 'release_status': 'REJECTED', 'finished': True})
            persist(fd, VERSION + '.json', record | {'phase': 'complete'})
        finally:
            os.close(fd)
    finally:
        if directory is not None:
            os.close(directory)
    print(f'{VERSION} rejected; published Core retained; combined app not accepted')
    return 1
