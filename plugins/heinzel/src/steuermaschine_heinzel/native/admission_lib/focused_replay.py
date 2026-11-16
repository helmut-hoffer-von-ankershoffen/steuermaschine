# /// script
# requires-python = ">=3.12"
# dependencies = ["coverage==7.17.0"]
# ///
"""Private adapter executed inside an archived protected reader checkout."""
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import types
import zipfile

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
package = types.ModuleType('scripts')
package.__path__ = [str(ROOT / 'scripts')]
package.__package__ = 'scripts'
sys.modules['scripts'] = package

from scripts import focused_gate as gate
from scripts.merge_gate import Gate, GITHUB, fetch_evidence


def digest(data):
    return hashlib.sha256(data).hexdigest()


def lines(data):
    for line in data.decode('utf-8-sig').splitlines():
        line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', line)
        match = re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z (.*)', line)
        if match:
            yield match[1]


def bind_environment(data, expected):
    records = {key: [] for key in expected}
    for line in lines(data):
        for key in expected:
            if line.startswith(f'  {key}: '):
                records[key].append(line.removeprefix(f'  {key}: '))
    gate.require(records == {key: [str(value)] for key, value in expected.items()},
                 'protected judge step environment does not bind the required proof')


def main():
    request = json.load(sys.stdin)
    original, live, paths = (request[key] for key in ('original', 'live', 'paths'))
    repo, token = request['repository'], request['token']

    def evidence(path, limit):
        return fetch_evidence(f'{GITHUB}/repos/{repo}/{path}', token, limit=limit)

    logs = {name: evidence(paths[name], 2 * 1024 * 1024) for name in ('checkout', 'coverage', 'report')}
    gate.require('##[group]Run uv run --no-build --frozen --script scripts/focused_gate.py' in lines(logs['coverage']),
                 'protected judge coverage command differs')
    checkout = list(lines(logs['checkout']))
    commits = [checkout[index + 1] for index, line in enumerate(checkout[:-1])
               if re.fullmatch(r'\[command\][^ ]*/git log -1 --format=%H', line)]
    gate.require(commits == [original['judge_source']], 'protected judge checkout source differs')
    bind_environment(logs['coverage'], dict(PR_HEAD_SHA=original['head'], PR_NUMBER=original['pr'], CI_RUN_ID=original['ci_run']))
    # The judge step names each posted check by its context in upper case (merge-gate -> MERGE_GATE).
    bind_environment(logs['report'], {name.upper().replace('-', '_'): live['checks'][name] for name in request['gate_checks']})
    artifacts = {}

    def read(path):
        data = evidence(path, gate.MAX_UPLOAD_LOG if path.endswith('/logs') else gate.MAX_ARCHIVE)
        if match := re.fullmatch(r'actions/artifacts/([1-9][0-9]*)/zip', path):
            key = match[1]
            gate.require(key not in artifacts or artifacts[key] == data, 'original archive bytes changed during replay')
            artifacts[key] = data
        return data

    manifest = json.dumps({'pull_request:' + original['head']: request['plan']}, sort_keys=True, separators=(',', ':'))
    verdict = gate.verify(Gate(repo, str(original['pr']), original['head'], token), original['ci_run'], manifest, read)
    gate.require(set(artifacts) == set(live['artifacts']), 'original archive inventory changed during replay')
    for key, data in artifacts.items():
        item = live['artifacts'][key]
        gate.require(len(data) == item['size_in_bytes'] and 'sha256:' + digest(data) == item['digest'],
                     'original archive metadata changed during replay')
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            report = gate.parse(archive.read(gate.REPORT))
        gate.require(report['commit'] == original['tested_commit'] and report['tree'] == original['tested_tree']
                     and report['base'] == original['tested_base'] and report['manifest_sha256'] == original['manifest_sha256'],
                     'original verified artifact does not bind the exact reviewed tested source')
    print(json.dumps(dict(tested_commit=original['tested_commit'], verdict=verdict, artifacts=live['artifacts'],
                          judge_log_sha256={key: digest(data) for key, data in logs.items()}), sort_keys=True))


if __name__ == '__main__':
    main()
