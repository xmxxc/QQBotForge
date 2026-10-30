"""Scheduled sync proposals use only GitHub's PR API, without pushing branches."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / '.github/scripts/sync-upstream.py'


def load_script():
    spec = importlib.util.spec_from_file_location('upstream_sync', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_new_upstream_commit_opens_cross_fork_pr():
    module = load_script()
    calls = []

    def api(method, path, body=None):
        calls.append((method, path, body))
        if '/compare/' in path:
            return {'ahead_by': 1, 'head_commit': {'sha': 'a' * 40}, 'base_commit': {'sha': 'b' * 40}}
        if method == 'GET':
            return []
        return {'number': 42, 'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}

    result = module.propose(api, 'https://github.com/xmxxc/QQBotForge/actions/runs/1')
    assert result == {'pr_number': 42, 'upstream_sha': 'a' * 40, 'base_sha': 'b' * 40}
    assert calls[-1][2]['head'] == 'ElainaCore:main'
    assert calls[-1][2]['base'] == 'main'
    assert 'Refs #100' in calls[-1][2]['body']
    assert all('/git/refs' not in call[1] for call in calls)


def test_no_new_upstream_commit_does_nothing():
    result = load_script().propose(lambda *_: {'ahead_by': 0}, 'run')
    assert result == {}


def test_existing_pr_is_reused():
    calls = []

    def api(method, path, body=None):
        calls.append(method)
        if '/compare/' in path:
            return {'ahead_by': 2}
        return [{'number': 19, 'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40}}]

    result = load_script().propose(api, 'run')
    assert result['pr_number'] == 19
    assert calls == ['GET', 'GET']
