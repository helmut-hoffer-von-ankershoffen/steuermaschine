"""Close an already-unfrozen published tail as failed; never publish or accept.

The coordinator's tail lock serializes local writers. Expected identities bind the
owner's decision to an immutable copy of the receipt and authenticated GETs. This
is not artifact provenance or a claim that a failed attempt was successful.
"""

import argparse
import base64
import os
import re
from datetime import datetime
from typing import Any

from . import rejection as legacy_archive

# The legacy archive/provider boundary is dynamically typed. Keep new helpers typed.
archive: Any = legacy_archive


def require(ok: object, message: str) -> None:
    if not ok:
        raise archive.InvalidRejection(message)


def timestamp(value: Any) -> None:
    require(isinstance(value, str), 'missing tail timestamp')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise archive.InvalidRejection('invalid tail timestamp') from None
    require(parsed.tzinfo is not None, 'tail timestamp needs timezone')


def decision_for(args: argparse.Namespace) -> dict[str, Any]:
    decision = {'kind': 'published-tail', 'version': args.version, 'receipt_sha256': args.expected_receipt_sha256,
                'token': args.expected_freeze_token, 'run': args.failed_compose_run,
                'attempt': args.failed_compose_attempt, 'head': args.expected_compose_head,
                'source': args.expected_core_source, 'reason': args.reason}
    for key, pattern in [('version', r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)'), ('receipt_sha256', r'[a-f0-9]{64}'),
                         ('token', r'[a-f0-9]{32}'), ('head', r'[a-f0-9]{40}'), ('source', r'[a-f0-9]{40}')]:
        require(isinstance(decision[key], str) and re.fullmatch(pattern, decision[key]), 'invalid tail identity')
    require(all(type(decision[k]) is int and decision[k] > 0 for k in ('run', 'attempt')), 'invalid compose attempt')
    require(isinstance(decision['reason'], str) and 10 <= len(decision['reason'].strip()) <= 4000,
            'exact reviewed tail rejection needs a written reason')
    return decision


def validate_original(data: dict[str, Any], decision: dict[str, Any]) -> None:
    allowed = {'version', 'freeze_token', 'freeze_lifted_at', 'compose', 'compose_run_id', 'compose_run_attempt',
               'compose_dispatch', 'compose_adopted', 'finished', 'hold_events'}
    require(not set(data) - allowed and (data.get('finished') is None or data.get('finished') is False), 'unsupported or finished tail receipt')
    require(data.get('version') == decision['version'] and data.get('freeze_token') == decision['token'],
            'tail receipt identity differs')
    require(data.get('compose_run_id') == decision['run'] and type(data.get('compose_run_id')) is int,
            'tail compose run differs')
    attempt = data.get('compose_run_attempt')
    require(type(attempt) is int and 0 < attempt <= decision['attempt'], 'invalid recorded compose attempt')
    timestamp(data.get('freeze_lifted_at'))
    timestamp(data.get('compose'))
    dispatch = data.get('compose_dispatch')
    expected = {'repo': archive.hzlib.PLUGINS, 'workflow': 'compose.yml', 'event': 'workflow_dispatch',
                'ref': 'main', 'sha': decision['head'],
                'inputs': {'version': decision['version'], 'core_wheel':
                           f"https://github.com/{archive.hzlib.CORE}/releases/download/v{decision['version']}/heinzel-{decision['version']}-py3-none-any.whl"}}
    require(dispatch == expected, 'tail dispatch identity differs')
    if 'compose_adopted' in data:
        adopted = data['compose_adopted']
        require(isinstance(adopted, dict) and set(adopted) == {'run', 'sha', 'created', 'at'}
                and type(adopted['run']) is int and adopted['run'] == decision['run']
                and adopted['sha'] == decision['head'], 'adopted compose identity differs')
        timestamp(adopted['created'])
        timestamp(adopted['at'])


def run_evidence(decision: dict[str, Any], attempt: int) -> dict[str, Any]:
    repo = archive.hzlib.PLUGINS
    run = archive.api(f"repos/{repo}/actions/runs/{decision['run']}/attempts/{attempt}")
    require(run.get('id') == decision['run'] and type(run.get('id')) is int
            and run.get('run_attempt') == attempt and type(run.get('run_attempt')) is int
            and run.get('repository', {}).get('full_name') == repo
            and run.get('head_repository', {}).get('full_name') == repo
            and run.get('head_sha') == decision['head'] and run.get('head_branch') == 'main'
            and run.get('path') == '.github/workflows/compose.yml' and run.get('event') == 'workflow_dispatch'
            and run.get('status') == 'completed' and run.get('conclusion') == 'failure',
            'compose attempt is not the exact completed failure')
    return {key: run[key] for key in ('id', 'run_attempt', 'head_sha', 'head_branch', 'path', 'event', 'status', 'conclusion')}


def evidence(decision: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    repo, tag = archive.hzlib.CORE, 'v' + decision['version']
    release = archive.api(f'repos/{repo}/releases/tags/{tag}')
    require(type(release.get('id')) is int and release['id'] > 0 and release.get('tag_name') == tag
            and release.get('draft') is False and release.get('prerelease') is False
            and isinstance(release.get('published_at'), str), 'Core release is not published and final')
    ref = archive.api(f'repos/{repo}/git/ref/tags/{tag}')
    require(ref.get('ref') == f'refs/tags/{tag}', 'published tag identity differs')
    obj = ref.get('object', {})
    for _ in range(4):
        require(isinstance(obj, dict) and isinstance(obj.get('sha'), str)
                and re.fullmatch('[a-f0-9]{40}', obj['sha']), 'invalid published tag object')
        if obj.get('type') == 'commit':
            break
        require(obj.get('type') == 'tag', 'published tag does not resolve to commit')
        obj = archive.api(f"repos/{repo}/git/tags/{obj['sha']}").get('object', {})
    require(obj.get('type') == 'commit' and obj.get('sha') == decision['source'], 'published source differs')
    attempts = [run_evidence(decision, n) for n in sorted({original['compose_run_attempt'], decision['attempt']})]
    latest = archive.api(f"repos/{archive.hzlib.PLUGINS}/actions/runs/{decision['run']}")
    require(type(latest.get('id')) is int and latest['id'] == decision['run']
            and type(latest.get('run_attempt')) is int and latest['run_attempt'] == decision['attempt']
            and latest.get('head_sha') == decision['head'] and latest.get('status') == 'completed'
            and latest.get('conclusion') == 'failure', 'compose run changed or was rerun')
    return {'release_id': release['id'], 'tag': tag, 'source': decision['source'], 'attempts': attempts}


def unchanged(rel: Any, raw: bytes) -> None:
    require(archive.private_read(rel.paths()[1]) == raw, 'tail receipt changed during rejection')
    require(not rel.owned_hold_exists(rel.state()['version']), 'tail hold appeared; preserving it')


def reject(rel: Any, args: argparse.Namespace) -> int:
    decision = decision_for(args)
    version = decision['version']
    require(rel.paths()[1] == rel.tail_receipt(version), 'only a published tail receipt may be rejected')
    records = archive.records()
    matching = [r for r in records if r['decision']['version'] == version]
    require(len(matching) <= 1 and not any(r['phase'] != 'complete' for r in records if r not in matching),
            'another rejection is pending')
    raw = archive.private_read(rel.paths()[1])
    data = archive.decode(raw)
    record = matching[0] if matching else None
    if record:
        require(record['decision'] == decision, 'rejection replay identity changed')
        original = archive.validate_record(record, version + '.json')
    else:
        require(archive.digest(raw) == decision['receipt_sha256'], 'tail receipt digest differs')
        original = data
    validate_original(original, decision)
    rel.hold_events(original)
    pending = original | {'rejection': decision, 'release_status': 'REJECTING', 'finished': False, 'accepted': False}
    final = original | {'rejection': decision, 'release_status': 'REJECTED', 'finished': True, 'accepted': False}
    require(data in (original, pending, final), 'receipt changed since archived decision')
    require(record is not None or data == original, 'missing rejection archive')
    unchanged(rel, raw)
    if record and record['phase'] == 'complete':
        # Absorption is a separate supported action; replay never removes its fields.
        require(data == final, 'completed rejection receipt changed')
        fd = archive.open_history()
        try:
            archive.persist(fd, version + '.json', record)
        finally:
            os.close(fd)
        return 1
    observed = evidence(decision, original)
    unchanged(rel, raw)
    fd = archive.open_history(create=True)
    try:
        if record is None:
            record = {'schema': 1, 'phase': 'archived', 'decision': decision,
                      'receipt_base64': base64.b64encode(raw).decode(), 'evidence': observed,
                      'hold_identity': None, 'hold_directory_identity': None}
            archive.persist(fd, version + '.json', record, initial=True)
        else:
            require(record['evidence'] == observed, 'rejection evidence changed')
            archive.persist(fd, version + '.json', record)
        rel.write_state(pending)
        pending_raw = archive.private_read(rel.paths()[1])
        require(evidence(decision, original) == observed, 'rejection evidence changed before completion')
        unchanged(rel, pending_raw)
        record = record | {'phase': 'unlink-authorized'}  # legacy archive phase; this path never unlinks anything
        archive.persist(fd, version + '.json', record)
        unchanged(rel, pending_raw)
        rel.write_state(final)
        archive.persist(fd, version + '.json', record | {'phase': 'complete'})
    finally:
        os.close(fd)
    print(f'{version} rejected; published Core retained; not accepted')
    return 1
