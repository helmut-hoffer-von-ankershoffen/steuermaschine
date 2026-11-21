"""Pure semantic validator for the generated Core release commit.

The tracked helper takes immutable blobs from its caller. It never imports the
release operator, executes candidate Python, or interprets the changelog review.
"""

import ast
from copy import deepcopy
import json
import re
import tomllib

VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')
PLUGIN = 'agent-skills/.claude-plugin/plugin.json'
ACCEPTANCE = 'scripts/acceptance/run_acceptance.py'
SCOPE = frozenset({'CHANGELOG.md', PLUGIN, 'copier.yml', 'pyproject.toml', ACCEPTANCE, 'uv.lock'})


class ReleaseScopeError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseScopeError(message)


def paths_from_diff(raw):
    records = raw.split(b'\0')
    require(records[-1] == b'' and len(records) % 2 == 1, 'invalid release diff records')
    paths = set()
    for header, path in zip(records[:-1:2], records[1:-1:2], strict=True):
        require(re.fullmatch(rb':(100644|100755) \1 [0-9a-f]{40} [0-9a-f]{40} M', header),
                'release diff must modify existing regular files with their original modes')
        name = path.decode()
        require(name in SCOPE and name not in paths, 'release diff contains an unsupported path')
        paths.add(name)
    require({'pyproject.toml', 'uv.lock'} <= paths, 'release diff needs project and root lock version updates')
    return paths


def same_value(old, new):
    """Compare parsed values without treating booleans, integers and floats alike."""
    if type(old) is not type(new):
        return False
    if isinstance(old, dict):
        return old.keys() == new.keys() and all(same_value(old[key], new[key]) for key in old)
    if isinstance(old, list):
        return len(old) == len(new) and all(same_value(a, b) for a, b in zip(old, new, strict=True))
    return old == new


def version_metadata(read, version):
    before = tomllib.loads(read('base', 'pyproject.toml').decode())
    after = tomllib.loads(read('head', 'pyproject.toml').decode())
    expected = deepcopy(before)
    project, bump = expected['project'], expected['tool']['bumpversion']
    old = project['version']
    require(project['name'] == 'heinzel' and isinstance(old, str) and VERSION.fullmatch(old)
            and bump['current_version'] == old and old != version, 'protected main release versions disagree')
    project['version'] = bump['current_version'] = version
    require(same_value(expected, after), 'pyproject changed beyond its Core version fields')
    lock = tomllib.loads(read('base', 'uv.lock').decode())
    roots = [package for package in lock['package'] if package['name'] == 'heinzel']
    require(len(roots) == 1 and roots[0]['version'] == old, 'protected main root lock version disagrees')
    roots[0]['version'] = version
    require(same_value(lock, tomllib.loads(read('head', 'uv.lock').decode())),
            'uv.lock changed beyond the Core root version')
    return old


def _unique_object(pairs):
    result = dict(pairs)
    require(len(result) == len(pairs), 'duplicate plugin metadata keys')
    return result


def _foreground(source):
    module = ast.parse(source)
    policies = [node.value for node in module.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == 'FOREGROUND_UI' for target in node.targets)]
    require(len(policies) == 1 and isinstance(policies[0], ast.Dict), 'unsupported acceptance foreground policy')
    return policies[0]


def _row(policy, version, *, optional=False):
    keys = [index for index, key in enumerate(policy.keys) if isinstance(key, ast.Constant) and key.value == version]
    if optional and not keys:
        return None
    require(len(keys) == 1 and ast.dump(policy.values[keys[0]]) == ast.dump(ast.parse('("ui", "serve")', mode='eval').body),
            'acceptance version needs the supported explicit foreground row')
    return policy.keys[keys[0]]


def generated_metadata(read, old, version):
    plugin = json.loads(read('base', PLUGIN), object_pairs_hook=_unique_object)
    require(isinstance(plugin, dict) and plugin.get('version') == old, 'protected main plugin version disagrees')
    plugin['version'] = version
    require(same_value(plugin, json.loads(read('head', PLUGIN), object_pairs_hook=_unique_object)),
            'plugin metadata changed beyond its version')

    def copier_row(value):
        return (f'heinzel_version:\n  type: str\n  default: "{value}" # bumped by the release process\n'
                '  when: false').encode()

    previous_copier = read('base', 'copier.yml')
    require(previous_copier.count(copier_row(old)) == 1
            and previous_copier.replace(copier_row(old), copier_row(version), 1) == read('head', 'copier.yml'),
            'copier changed beyond its generated Core version')

    before, after = (read(side, ACCEPTANCE) for side in ('base', 'head'))
    old_policy, new_policy = _foreground(before), _foreground(after)
    _row(old_policy, old)
    old_row = _row(old_policy, version, optional=True)
    new_row = _row(new_policy, version)
    if old_row is not None:
        require(before == after, 'acceptance source changed despite an existing supported row')
        return
    # A byte comparison outside the generated literal row also keeps comments,
    # encoding and script metadata out of the release-only edit.
    lines = after.splitlines(keepends=True)
    index = new_row.lineno - 1
    require(lines[index].rstrip(b'\r\n') == f'    "{version}": ("ui", "serve"),'.encode()
            and b''.join(lines[:index] + lines[index + 1:]) == before,
            'acceptance source changed beyond its generated foreground row')


def validate(read, raw, version):
    """Return the previous version and exact modified paths after semantic validation."""
    require(isinstance(version, str) and VERSION.fullmatch(version), 'expected a canonical Core release version')
    paths = paths_from_diff(raw)
    try:
        old = version_metadata(read, version)
        generated_metadata(read, old, version)
    except (KeyError, TypeError, SyntaxError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseScopeError('invalid generated release metadata') from exc
    return old, paths
