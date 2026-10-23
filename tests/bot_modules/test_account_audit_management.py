"""Account management adapter contracts through actual page JavaScript and authenticated HTTP."""

import json
import subprocess
from pathlib import Path

from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_account_c01 import bind


async def ready(h, member='member-a', group='group-a'):
    await h.account_api('POST', 'access', {'operation': 'whitelist', 'group': group, 'enabled': True})
    await h.state.confirm_member('bot-a', group, member, evidence='checked', member_role='member')


async def test_page_payloads_save_and_partial_retry(account_host):
    h = account_host
    a = await h.create()
    await ready(h)
    requests = json.loads(subprocess.check_output(['node', str(Path(__file__).with_name('account_page.cjs')), a['id']], text=True))
    assert len(requests) == 4
    single, bulk, revoke, expiry = [r['body'] for r in requests]
    status, result = await h.account_api('POST', 'access', single)
    assert status == 200, result
    assert (await h.access.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', a['id']))['allowed']
    status, result = await h.account_api('POST', 'access', bulk)
    assert status == 200 and result['data']['summary'] == {'success': 1, 'failed': 1}, result
    saved = result['data']['items'][0]['result']['id']
    await ready(h, 'missing-member')
    retry = {**bulk, 'members': [bulk['members'][1]]}
    result = (await h.account_api('POST', 'access', retry))[1]['data']
    assert result['summary'] == {'success': 1, 'failed': 0}
    assert saved in [s['id'] for s in (await h.access.access_snapshot('bot-a', 'account_sharing'))['grants']]
    for data in (expiry, revoke):
        status, result = await h.account_api('POST', 'access', data)
        assert status == 200 and result['data']['summary'] == {'success': 2, 'failed': 0}, result
    assert (await h.account_api('POST', 'access', single, authenticated=False))[0] == 401
    response = await h.http.post(
        '/api/ext/account-sharing/access?bot=bot-a', json=single, headers={'Cookie': 'elaina_session=group-test', 'Origin': 'https://foreign.invalid'}
    )
    assert response.status == 403
    assert (await h.account_api('GET', 'access', bot='bot-b'))[1]['data']['grants'] == []


async def test_group_deny_is_group_wide_and_allow_does_not_grant(account_host):
    h = account_host
    a = await h.create(name='保密名称')
    for member in ('member-a', 'member-b'):
        await ready(h, member)
        await h.account_api('POST', 'access', {'operation': 'grant', 'group': 'group-a', 'member': member, 'resources': [a['id']]})
    await bind(h, 'group-a', 'member-b')
    status, result = await h.account_api('POST', 'access', {'operation': 'deny', 'scope': 'group', 'group': 'group-a', 'member': 'member-a'})
    assert status == 200, result
    snapshot = await h.access.access_snapshot('bot-a', 'account_sharing')
    assert snapshot['denies'][0]['member'] == ''
    for member in ('member-a', 'member-b'):
        assert not (await h.access.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', member, a['id']))['allowed']
    await h.event('账户列表', user='private-a')
    await h.event('账户列表', user='member-b', group='group-a')
    assert all('保密名称' not in t for t in h.texts())
    await h.account_api('POST', 'access', {'operation': 'revoke', 'group': 'group-a', 'member': 'member-b'})
    await h.account_api('POST', 'access', {'operation': 'allow', 'scope': 'group', 'group': 'group-a', 'member': 'member-a'})
    assert not (await h.access.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-b', a['id']))['allowed']
    assert (await h.access.evaluate('bot-a', 'account_sharing', 'account_sharing', 'group-a', 'member-a', a['id']))['allowed']


async def test_delete_cleans_only_own_resource_rules_and_can_retry_cleanup(account_host, monkeypatch):
    h = account_host
    a, b = await h.create(), await h.create()
    ids = []
    for target in (a['id'], b['id']):
        ids.append((await h.account_api('POST', 'rules', {'name': 'same words', 'target': target, 'keywords': ['same words']}))[1]['data']['id'])
    qa = h.modules.get('self_qa')
    public = await qa.manage('bot-a', 'public', 'create', {'name': 'ordinary', 'kind': 'text', 'target': 'ordinary answer', 'keywords': ['same words']})
    status, result = await h.account_api('DELETE', 'accounts/' + a['id'])
    assert status == 200, result
    rules = (await h.account_api('GET', 'rules'))[1]['data']
    assert [r['id'] for r in rules] == [ids[1]]
    assert await qa.manage('bot-a', 'public', 'get', key=public['id'])
    original = qa.manage

    async def failed(bot, namespace, operation, *args, **kwargs):
        if operation == 'delete':
            raise RuntimeError('temporary cleanup failure')
        return await original(bot, namespace, operation, *args, **kwargs)

    monkeypatch.setattr(qa, 'manage', failed)
    status, result = await h.account_api('DELETE', 'accounts/' + b['id'])
    assert status == 503 and b['id'] in result['error'], result
    assert (await h.account_api())[1]['data'] == []
    await h.event('same words', user='stranger', group='group-a')
    assert h.texts('group-a') == []
    monkeypatch.setattr(qa, 'manage', original)
    assert (await h.account_api('DELETE', 'accounts/' + b['id']))[0] == 200
    assert (await h.account_api('GET', 'rules'))[1]['data'] == []


async def test_http_report_is_page_renderable_without_credentials(account_host):
    h = account_host
    a = await h.create()
    await ready(h)
    status, result = await h.account_api('POST', 'group-delivery', {'group': 'group-a', 'resource': a['id'], 'enabled': True})
    assert status == 200, result
    assert 'credential' not in json.dumps(result).lower()
    mode = result['data']
    assert mode['report']['complete'] is False and mode['report']['failures_complete'] is False
    assert mode['report']['members'][0]['actions']['invoke']['reason'] == 'no_explicit_permission'
    raw = subprocess.check_output(['node', str(Path(__file__).with_name('test_account_page.cjs')), json.dumps(mode)], text=True)
    rendered = json.loads(raw.splitlines()[0])
    assert json.loads(rendered['summary'])['名单完整'] is False
    assert 'no_explicit_permission' in rendered['rows'][0] and 'undefined' not in rendered['rows'][0]
    assert (await h.account_api('POST', 'group-delivery', {'group': 'group-a', 'resource': a['id'], 'enabled': True, 'force': True}))[0] == 422


async def test_deny_subject_ranges_and_consumer_isolation(account_host):
    h = account_host
    a = await h.create()
    for g, m in [('group-a', 'member-a'), ('group-a', 'member-b'), ('group-b', 'member-a')]:
        await ready(h, m, g)
        await h.account_api('POST', 'access', {'operation': 'grant', 'group': g, 'member': m, 'resources': [a['id']]})
    await bind(h, 'group-a', 'member-a')
    acl = h.access
    await acl.declare('bot-a', 'self_qa', owner='module:self_qa', functions={'read': {'kind': 'public', 'groups': ['group-a']}})

    async def allowed(g, m):
        return (await acl.evaluate('bot-a', 'account_sharing', 'account_sharing', g, m, a['id']))['allowed']

    await h.account_api('POST', 'access', {'operation': 'deny', 'scope': 'member', 'group': 'group-a', 'member': 'member-a'})
    assert not await allowed('group-a', 'member-a') and await allowed('group-a', 'member-b') and await allowed('group-b', 'member-a')
    await h.account_api('POST', 'access', {'operation': 'allow', 'scope': 'member', 'group': 'group-a', 'member': 'member-a'})
    # A private-native denial blocks the mapped private recipient, not group members.
    await h.account_api('POST', 'access', {'operation': 'deny', 'member': 'private-a'})
    assert await allowed('group-a', 'member-a')
    assert not (await acl.private_contexts('bot-a', 'account_sharing', 'account_sharing', 'private-a'))['selected']
    await h.account_api('POST', 'access', {'operation': 'deny', 'scope': 'group', 'group': 'group-a', 'member': 'member-a'})
    assert not await allowed('group-a', 'member-b') and await allowed('group-b', 'member-a')
    assert (await acl.evaluate('bot-a', 'self_qa', 'read', 'group-a', 'member-b'))['allowed']
    assert (await h.account_api('GET', 'access', bot='bot-b'))[1]['data']['denies'] == []
