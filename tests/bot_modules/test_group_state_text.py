"""#62: owner QQ replies name the operation and explain facts in Chinese; groups only get a short status."""

import json
import re
from pathlib import Path

import pytest
from socket_platform import until
from test_group_roster import roster_platform


async def reply(host, text, **kwargs):
    host.sender.messages.clear()
    await host.event(text, **kwargs)
    assert len(host.sender.messages) == 1, host.sender.messages
    return host.sender.messages[0][2]


def assert_readable(text):
    # QQ swallows '_' and owners cannot read raw objects.
    assert '{' not in text and '"' not in text, text
    assert not re.search(r'observed_at|checked_at|reliable|member_role', text), text


async def test_each_owner_command_has_its_own_title_and_chinese_summary(group_host):
    host = group_host
    roster_platform(host)
    await host.event('普通聊天', user='member-a', group='group-a')

    overview = await reply(host, '群状态')
    assert overview.startswith('群状态概览（仅已知群，不代表全部群）')
    assert '恢复基线：已就绪' in overview and '已知群 1 个' in overview
    assert 'group-a：历史（收到新群事件，需刷新）' in overview and 'Bot 在群状态未知' in overview
    assert '已知成员 1 人，可靠 0 人' in overview
    assert_readable(overview)

    refreshed = await reply(host, '群状态 刷新 group-a')
    assert refreshed.startswith('群刷新结果\n群 group-a：可靠（已核验）')
    # allow_proactive_msg=False is a known false, unlike a missing value.
    assert 'Bot 在群（管理员），不允许主动消息' in refreshed and '最近查询 未查询' not in refreshed
    assert_readable(refreshed)
    # Rebuild verifies the known group and member-a, both answerable by the fake platform.
    assert await reply(host, '群状态 恢复') == '恢复基线重建：已完成，事实恢复可用'

    roster = await reply(host, '群状态 名册 group-a')
    assert roster.startswith('群名册核验\n群 group-a：名册完整')
    assert '本轮读到 3 人（可作全群名单）' in roster
    assert 'this-bot：管理员，本 Bot' in roster and 'other-bot：成员，Bot' in roster and 'member-a：成员' in roster
    assert_readable(roster)

    verified = await reply(host, '群状态 核验 group-a member-a')
    assert verified.startswith('成员核验结果\n群 group-a 成员 member-a：可靠（已核验），群主')
    assert_readable(verified)

    confirmed = await reply(host, '群状态 确认 {"group":"group-a","member":"member-z","evidence":"主人已核对"}')
    assert confirmed.startswith('成员人工确认\n群 group-a 成员 member-z：可靠（已核验），角色未知')
    assert_readable(confirmed)

    # The JSON machine API keeps its fields and types.
    data = (await (await host.api()).json())['data']
    assert data['scope'] == 'known' and data['complete'] is False and isinstance(data['groups'][0]['reliable'], bool)
    assert data['groups'][0]['bot_state'] == {'member_role': 'admin', 'recv_msg_setting': 'all', 'allow_proactive_msg': False, 'in_group': True}


@pytest.mark.parametrize(
    'response,expected',
    [
        ((None, {'code': '11253'}), '无权限（平台无权限）'),
        (None, '未知（平台返回无法识别）'),
        (({'member_role': 'admin', 'recv_msg_setting': 'all'}, None), '未知（平台返回无法识别）'),
    ],
)
async def test_failed_refresh_is_not_reported_as_success_or_absence(group_host, response, expected):
    host = group_host
    await host.event('群状态 刷新 group-a')
    host.sender.response = response
    text = await reply(host, '群状态 刷新 group-a')
    assert text.startswith('群刷新结果\n群 group-a：' + expected)
    assert '可靠' not in text and '已完成' not in text
    # History is labelled as history; a failed query never says the Bot left.
    assert '历史记录：Bot 在群（管理员）' in text and '离群' not in text


async def test_failed_roster_and_member_verification_name_the_reason(group_host):
    host = group_host
    roster = await reply(host, '群状态 名册 group-a')
    assert roster.startswith('群名册核验\n群 group-a：名册不完整，原因：平台无权限')
    assert '仅本轮观察，不能作全群名单' in roster and '本轮读到 0 人' in roster
    member = await reply(host, '群状态 核验 group-a member-a')
    assert member.startswith('成员核验结果\n群 group-a 成员 member-a：无权限（平台无权限），角色未知，是否 Bot 未知')


async def test_recovery_barrier_is_never_reported_as_success(group_host):
    host, state = group_host, group_host.state
    await host.event('群状态 刷新 group-a')
    host.lifecycle('GROUP_MEMBER_REMOVE', member='member-c', sequence=2, event_id=None, timestamp=1002)
    await until(lambda: state.recovery.get_status('bot-a').failed)
    overview = await reply(host, '群状态')
    # The framework may replace the reason (e.g. with the delivery outcome); the next step stays a rebuild.
    assert re.search(r'恢复基线：未就绪（需重建，请发“群状态 恢复”或在后台重建；原因：[^（）]+）', overview), overview
    assert 'group-a：可靠' not in overview
    host.sender.response = (None, {'code': '11253'})
    rebuilt = await reply(host, '群状态 恢复')
    assert rebuilt.startswith('恢复基线重建：未完成，事实仍暂停使用')
    assert '恢复基线：未就绪（需重建' in await reply(host, '群状态')
    assert '已完成' not in rebuilt


@pytest.mark.parametrize(
    'command,title',
    [
        ('群状态', '群状态概览'),
        ('群状态 刷新 group-a', '群刷新结果'),
        ('群状态 名册 group-a', '群名册核验'),
        ('群状态 核验 group-a member-a', '成员核验结果'),
        ('群状态 恢复', '恢复基线重建'),
    ],
)
async def test_group_chat_only_gets_a_short_status_for_the_operation(group_host, command, title):
    host = group_host
    roster_platform(host)
    await host.event('群状态 名册 group-a')
    text = await reply(host, command, group='group-a')
    assert text == f'{title}已处理，详情请在主人私聊或已登录后台查看'
    for detail in ('member-a', 'this-bot', 'other-bot', '可靠', '无权限'):
        assert detail not in text


def test_page_and_reply_share_one_reason_table():
    from modules.group_state.text import REASONS

    html = (Path(__file__).absolute().parents[2] / 'modules/group_state/page.html').read_text(encoding='utf-8')
    page = json.loads(re.search(r'const REASONS=(\{.*?\});', html).group(1))
    assert page == REASONS


@pytest.mark.parametrize('fact,expected', json.loads((Path(__file__).with_name('group_state_labels.json')).read_text(encoding='utf-8')))
def test_labels_keep_unknown_progress_and_failure_apart(fact, expected):
    # The page test checks the same cases, so the two label rules cannot drift apart.
    from modules.group_state.text import label

    assert label(fact) == expected


def test_unknown_fields_stay_unknown_and_recovery_names_the_next_step():
    from modules.group_state.text import bot_state, recovery_line

    assert bot_state({'reliable': True, 'bot_state': {'member_role': None}}) == 'Bot 在群（角色未知），主动消息未知'
    assert (
        recovery_line({'ready': False, 'phase': 'processing', 'reason': 'unobserved_dispatch'})
        == '恢复基线：未就绪（处理中，请稍后再查看；原因：有未处理的事件）'
    )
    # The module's own verdict wins over the phase (e.g. an HTTP callback was received).
    assert recovery_line({'ready': False, 'phase': 'processing', 'reason': ''}, 'recovery_required').startswith('恢复基线：未就绪（需重建')
    assert recovery_line({'ready': True}, 'persistence_pending') == '恢复基线：未就绪（处理中，请稍后再查看）'
