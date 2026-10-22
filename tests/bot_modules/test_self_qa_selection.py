"""Regex rules, unified ranking and caller-bound candidate menus, from message to reply."""

import asyncio
import time

from test_self_qa import qa_host as qa_host


async def setup(host, *rules, bot='bot-a', groups=('group-a', 'group-b'), private=True):
    status, data = await host.api({'operation': 'settings', 'groups': list(groups), 'private': private}, bot=bot)
    assert status == 200, data
    created = []
    for rule in rules:
        status, data = await host.api({'operation': 'create', **rule}, bot=bot)
        assert status == 200, data
        created.append(data['data'])
    return created


async def test_exact_beats_contains_beats_regex_and_priority_and_length(qa_host):
    host = qa_host
    await setup(
        host,
        {'name': '正则', 'target': 'R', 'mode': 'regex', 'keywords': [r'^价格\d*$']},
        {'name': '包含短', 'target': 'C1', 'mode': 'contains', 'keywords': ['价']},
        {'name': '包含长', 'target': 'C2', 'mode': 'contains', 'keywords': ['价格']},
        {'name': '精确', 'target': 'E', 'keywords': ['价格']},
    )
    await host.event('价格')
    await host.event('价格表')
    await host.event('价格123')
    assert [m[2] for m in host.replies()] == ['E', 'C2', 'C2']
    # Explicit priority beats keyword length among contains rules.
    rules = (await host.api())[1]['data']['rules']
    short = next(r for r in rules if r['name'] == '包含短')
    await host.api({'operation': 'update', 'id': short['id'], 'priority': 5})
    await host.event('价格表')
    assert [m[2] for m in host.replies()] == ['C1']
    status, data = await host.api({'operation': 'test', 'text': '价格9', 'group': ''})
    assert status == 200 and [m['name'] for m in data['data']['matches']] == ['包含短', '包含长', '正则']
    for rule in rules:
        if rule['mode'] == 'contains':
            await host.api({'operation': 'update', 'id': rule['id'], 'enabled': False})
    await host.event('价格9')
    assert [m[2] for m in host.replies()] == ['R']


async def test_invalid_regex_rejected_with_feedback_and_old_rule_kept(qa_host):
    host = qa_host
    (rule,) = await setup(host, {'name': '号码', 'target': 'N', 'mode': 'regex', 'keywords': [r'^\d+$']})
    status, data = await host.api({'operation': 'create', 'name': '坏', 'target': 'x', 'mode': 'regex', 'keywords': ['(']})
    assert status == 422 and '正则无效' in data['error']
    status, data = await host.api({'operation': 'update', 'id': rule['id'], 'keywords': ['[a-']})
    assert status == 422
    assert (await host.api({'operation': 'create', 'name': '长', 'target': 'x', 'mode': 'regex', 'keywords': ['a' * 501]}))[0] == 422
    await host.event('12345')
    assert [m[2] for m in host.replies()] == ['N']
    saved = (await host.api())[1]['data']['rules']
    assert len(saved) == 1 and saved[0]['keywords'][0]['text'] == r'^\d+$'


async def test_runaway_regex_is_bounded_and_does_not_block_other_bots(qa_host):
    host = qa_host
    await setup(
        host,
        {'name': '慢', 'target': 'SLOW', 'mode': 'regex', 'keywords': [r'^(a+)+$']},
        {'name': '包含', 'target': 'OK', 'mode': 'contains', 'keywords': ['aaaa']},
    )
    await setup(host, {'name': '帮助', 'target': 'B', 'keywords': ['帮助']}, bot='bot-b')
    evil = 'a' * 40 + 'b'
    started = time.perf_counter()
    slow = asyncio.create_task(host.event(evil))
    await asyncio.sleep(0.05)
    other = time.perf_counter()
    await host.event('帮助', bot='bot-b')
    assert time.perf_counter() - other < 0.3
    await slow
    assert time.perf_counter() - started < 2
    # The runaway regex counts as not matched; the contains rule still answers.
    assert [m[2] for m in host.replies()] == ['B', 'OK']
    # The killed worker is replaced on the next request.
    status, data = await host.api({'operation': 'test', 'text': 'aaaa'})
    assert [m['name'] for m in data['data']['matches']] == ['包含', '慢']


async def test_ties_offer_caller_bound_menu_and_click_rereads_rules(qa_host):
    host = qa_host
    first, second, alias = await setup(
        host,
        {'name': '营业时间', 'target': '九点到五点', 'mode': 'contains', 'keywords': ['时间'], 'priority': 1},
        {'name': '活动时间', 'target': '周末', 'mode': 'contains', 'keywords': ['时间'], 'priority': 1, 'groups': ['group-a']},
        {'name': '同答案', 'target': '周末', 'mode': 'contains', 'keywords': ['时间', '几点'], 'priority': 1},
    )
    host.sender.buttons.clear()
    await host.event('几点 时间', group='group-a')
    menu = host.replies()[-1][2]
    assert '营业时间' in menu and '活动时间' in menu and '同答案' not in menu
    rows = host.sender.buttons[-1]
    assert all(button['list'] == ['member-a'] for row in rows for button in row)
    pick = rows[1][0]['data']
    # Another member cannot use the caller's menu; ACK is still sent.
    assert await host.click(pick, user='member-b', group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '这不是你的选择菜单。')]
    assert await host.click(pick, user='member-a', group='group-b') == 0
    assert host.replies() == [('bot-a', 'group-b', '这不是你的选择菜单。')]
    assert await host.click(pick, group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '周末')]
    # Disabling, rescoping or deleting the rule makes the old menu entry stale.
    await host.api({'operation': 'update', 'id': second['id'], 'groups': ['group-b']})
    assert await host.click(pick, group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '该答案已失效，请重新提问。')]
    await host.api({'operation': 'update', 'id': second['id'], 'groups': ['group-a']})
    await host.api({'operation': 'delete', 'id': alias['id']})
    assert await host.click(pick, group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '周末')]
    await host.api({'operation': 'delete', 'id': second['id']})
    assert await host.click(pick, group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '该答案已失效，请重新提问。')]
    assert await host.click('qa:' + '0' * 32 + ':0', group='group-a') == 0
    assert host.replies() == [('bot-a', 'group-a', '菜单已失效，请重新提问。')]


async def test_text_choice_when_buttons_rejected_and_paging(qa_host):
    host = qa_host
    rules = [{'name': f'答案{i}', 'target': f'T{i}', 'mode': 'contains', 'keywords': ['问'], 'priority': 2} for i in range(7)]
    await setup(host, *rules)
    host.sender.reject_buttons = True
    await host.event('问一下')
    text = host.replies()[-1][2]
    assert '1. 答案0' in text and '答案5' not in text and '按钮暂不可用' in text and '第 1/2 页' in text
    await host.event('问答选择 页 2')
    text = host.replies()[-1][2]
    assert '6. 答案5' in text and '7. 答案6' in text
    await host.event('问答选择 7')
    assert host.replies() == [('bot-a', 'member-a', 'T6')]
    # Another private user has no menu here: their text falls through to the framework.
    await host.event('问答选择 1', user='member-b')
    assert host.replies() == []
    await host.event('问答选择 x')
    assert '请发送' in host.replies()[-1][2]
    host.sender.reject_buttons = False
    await host.event('问一下')
    rows = host.sender.buttons[-1]
    assert rows[-1][0]['text'] == '下一页'
    assert await host.click(rows[-1][0]['data']) == 0
    assert '6. 答案5' in host.replies()[-1][2]
