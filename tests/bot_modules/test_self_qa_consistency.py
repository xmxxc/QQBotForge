"""Rules that change while an answer is being prepared are never answered from the old copy."""

from test_self_qa import configure
from test_self_qa import qa_host as qa_host

SLOW = {'operation': 'create', 'name': '慢', 'target': 'R', 'mode': 'regex', 'keywords': ['^zz$']}


async def help_rule(host):
    await configure(host, groups=('group-a', 'group-b'))
    assert (await host.api(SLOW))[0] == 200
    return next(r for r in (await host.api())[1]['data']['rules'] if r['name'] == '帮助')


def during_regex(host, *changes):
    """Run one owner change after each real regex await returns (the wait every regex rule adds)."""
    qa = host.modules.get('self_qa')
    original = qa.regex.search
    pending = list(changes)

    async def search(*args):
        result = await original(*args)
        if pending:
            await host.api(pending.pop(0))
        return result

    qa.regex.search = search
    return lambda: setattr(qa.regex, 'search', original)


async def test_public_answer_unchanged_during_wait_is_sent(qa_host):
    host = qa_host
    await help_rule(host)
    restore = during_regex(host, {'operation': 'settings', 'groups': ['group-a', 'group-b'], 'private': True})
    await host.event('帮助', group='group-a')
    restore()
    # A no-op change re-resolves once and still answers from the current rule.
    assert host.replies() == [('bot-a', 'group-a', '公开说明')]


async def test_public_rule_disabled_deleted_or_rescoped_during_wait_is_not_answered(qa_host):
    host = qa_host
    rule = await help_rule(host)
    for change, undo in (
        ({'enabled': False}, {'enabled': True}),
        ({'groups': ['group-b']}, {'groups': []}),
    ):
        restore = during_regex(host, {'operation': 'update', 'id': rule['id'], **change})
        await host.event('帮助', group='group-a')
        restore()
        assert host.replies() == [], change
        await host.api({'operation': 'update', 'id': rule['id'], **undo})
    restore = during_regex(host, {'operation': 'delete', 'id': rule['id']})
    await host.event('帮助', group='group-a')
    restore()
    assert host.replies() == []


async def test_public_rule_edited_during_wait_answers_current_content(qa_host):
    host = qa_host
    rule = await help_rule(host)
    restore = during_regex(host, {'operation': 'update', 'id': rule['id'], 'target': '新说明'})
    await host.event('帮助', group='group-a')
    restore()
    assert host.replies() == [('bot-a', 'group-a', '新说明')]


async def test_scope_disabled_during_wait_is_not_answered(qa_host):
    host = qa_host
    await help_rule(host)
    restore = during_regex(host, {'operation': 'settings', 'groups': ['group-b'], 'private': True})
    await host.event('帮助', group='group-a')
    restore()
    assert host.replies() == []


async def test_rules_that_keep_changing_get_status_not_old_content(qa_host):
    host = qa_host
    rule = await help_rule(host)
    edits = [{'operation': 'update', 'id': rule['id'], 'target': f'版本{i}'} for i in range(10)]
    restore = during_regex(host, *edits)
    await host.event('帮助', group='group-a')
    restore()
    replies = host.replies()
    assert replies == [('bot-a', 'group-a', '答案正在更新，请稍后重新提问。')]


async def test_menu_choice_and_titles_follow_rule_changes_during_wait(qa_host):
    host = qa_host
    await configure(host, groups=('group-a',))
    for name, target in (('甲', 'A'), ('乙', 'B')):
        await host.api({'operation': 'create', 'name': name, 'target': target, 'keywords': ['问'], 'mode': 'contains', 'priority': 1})
    assert (await host.api(SLOW))[0] == 200
    rules = {r['name']: r for r in (await host.api())[1]['data']['rules']}
    # Title renamed while the menu is being prepared: the menu shows the current title.
    restore = during_regex(host, {'operation': 'update', 'id': rules['甲']['id'], 'name': '甲新'})
    host.sender.buttons.clear()
    await host.event('问', group='group-a')
    restore()
    menu = host.replies()[-1][2]
    assert '甲新' in menu and '1. 甲\n' not in menu
    pick = next(row[0]['data'] for row in host.sender.buttons[-1] if row[0]['text'] == '甲新')
    # Disabled while a click is re-reading the rules: no old answer.
    restore = during_regex(host, {'operation': 'update', 'id': rules['甲']['id'], 'enabled': False})
    assert await host.click(pick, group='group-a') == 0
    restore()
    assert host.replies() == [('bot-a', 'group-a', '该答案已失效，请重新提问。')]
    # Edited while a click is re-reading: the current content is sent.
    await host.api({'operation': 'update', 'id': rules['甲']['id'], 'enabled': True})
    restore = during_regex(host, {'operation': 'update', 'id': rules['甲']['id'], 'target': 'A2'})
    assert await host.click(pick, group='group-a') == 0
    restore()
    assert host.replies() == [('bot-a', 'group-a', 'A2')]
