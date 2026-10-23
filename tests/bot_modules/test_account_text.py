"""#62: account owner to-dos, results and rules read as Chinese summaries; empty credential fields add no lines."""

import json
import re

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_account_audit_management import ready


async def owner(host, text, **kwargs):
    await host.event('账户管理 ' + text, user='owner-a', **kwargs)
    return host.texts()


@pytest.mark.parametrize(
    'fields,expected',
    [
        ({}, '共享账户\n账号：fake-login\n密码：fake-secret'),
        ({'description': '', 'url': ''}, '共享账户\n账号：fake-login\n密码：fake-secret'),
        ({'description': '周末用', 'url': ''}, '共享账户\n账号：fake-login\n密码：fake-secret\n周末用'),
        ({'description': '', 'url': 'https://fake.invalid/login'}, '共享账户\n账号：fake-login\n密码：fake-secret\nhttps://fake.invalid/login'),
        ({'description': '周末用', 'url': 'https://fake.invalid/login'}, '共享账户\n账号：fake-login\n密码：fake-secret\n周末用\nhttps://fake.invalid/login'),
    ],
)
async def test_credentials_omit_empty_description_and_url(account_host, fields, expected):
    host = account_host
    account = await host.create(**fields)
    assert (await owner(host, f'预览 {account["id"]}')) == [expected]


async def test_owner_todos_results_and_rules_are_chinese_summaries(account_host):
    host = account_host
    account = await host.create()
    await ready(host)
    bulk = {'operation': 'bulk_grant', 'group': 'group-a', 'members': [{'member': 'member-a'}, {'member': 'missing-member'}], 'resources': [account['id']]}
    status, result = await host.account_api('POST', 'access', bulk)
    assert status == 200 and result['data']['summary'] == {'success': 1, 'failed': 1}, result
    host.texts()

    todos = (await owner(host, '待办'))[-1]
    assert todos.startswith('账户管理待办（共 ')
    assert '待身份绑定：群 group-a 成员 member-a' in todos
    assert re.search(
        r'主人通知未送达：\S+ \S+ 批量授权 \w{8}（群 group-a）：授权部分成功（成功 1，失败 1）；绑定待处理；发送未执行；通知无法执行；处理：账户管理 重试通知 \w+',
        todos,
    )
    assert '"type"' not in todos and "'type'" not in todos

    results = (await owner(host, '结果'))[-1]
    assert results.startswith('账户管理结果（共 1 条')
    # Partial success and not-executed delivery are named as such, never as success.
    assert '批量授权' in results and '授权部分成功（成功 1，失败 1）' in results and '发送未执行' in results
    assert '{' not in results

    await owner(host, '规则新增 ' + json.dumps({'name': '周末号', 'target': account['id'], 'keywords': ['周末']}, ensure_ascii=False))
    rules = (await owner(host, '规则列表'))[-1]
    assert rules.startswith('账户规则（共 1 条）')
    assert '周末号：启用；精确匹配：周末；指向账户 共享账户' in rules
    assert '{' not in rules and 'fake-secret' not in rules

    # Group chats still only get the short status.
    for command in ('待办', '结果', '规则列表'):
        replies = await owner(host, command, group='group-a')
        assert replies and all('member-a' not in text and '周末' not in text and '部分成功' not in text for text in replies), replies


async def test_empty_todos_and_results_say_so(account_host):
    host = account_host
    assert await owner(host, '待办') == ['账户管理待办：暂无']
    assert await owner(host, '结果') == ['账户管理结果：暂无']
    assert await owner(host, '规则列表') == ['账户规则：暂无']
