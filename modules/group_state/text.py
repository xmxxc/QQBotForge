"""Chinese owner replies for group facts; the JSON machine API keeps the raw fields."""

import time

# page.html carries the same table as `const REASONS=`; a test keeps the two equal.
REASONS = {
    'not_verified': '尚未核验',
    'unknown_group': '未知群',
    'refreshing': '正在刷新',
    'bot_state_refreshing': '正在刷新 Bot 状态',
    'bot_state_refreshed': 'Bot 状态已刷新',
    'roster_refreshing': '正在核验名册',
    'roster_refreshed': '名册已核验',
    'verifying': '正在核验',
    'confirming': '正在记录确认',
    'individual_verification': '单人核验后需重核名册',
    'observed_event': '收到新群事件',
    'observed_event_requires_refresh': '收到新群事件，需刷新',
    'requires_verification': '需核验',
    'join_requires_verification': '新入群，需核验',
    'process_restart': '重启或重载后需重新核验',
    'state_changed': '核验期间状态有变化',
    'persistence_pending': '正在保存',
    'persistence_failed': '保存失败',
    'recovery_pending': '恢复基线未完成',
    'recovery_processing': '恢复处理中',
    'recovery_required': '需重建恢复基线',
    'rebuilding': '正在重建恢复基线',
    'consumer_failed': '消费通知失败',
    'permission_denied': '平台无权限',
    'query_failed': '查询失败',
    'timeout': '超时',
    'response_unknown': '平台返回无法识别',
    'unsupported': '当前 Bot 不支持查询',
    'refresh_failed': '刷新失败',
    'roster_failed': '名册核验失败',
    'verification_failed': '核验失败',
    'confirmation_failed': '确认失败',
    'refresh_cancelled': '刷新已取消',
    'roster_cancelled': '名册核验已取消',
    'verification_cancelled': '核验已取消',
    'confirmation_cancelled': '确认已取消',
    'empty_roster': '平台返回空名册',
    'role_missing': '缺少角色',
    'stale_membership': '部分成员角色未知',
    'known_member_missing': '已知成员不在名册中',
    'pagination_conflict': '分页数据冲突',
    'pagination_limit': '分页超出上限',
    'bot_identity_unknown': '无法确认本 Bot 群内身份',
    'bot_identity_conflict': '本 Bot 身份与名册矛盾',
    'lifecycle_changed': '群成员变化',
    'bot_added_requires_baseline': 'Bot 入群，需重建恢复基线',
    'bot_removed': 'Bot 已被移出',
    'missing_native_group': '事件缺少群标识',
    'late_event': '事件迟到',
    'same_time_conflict': '同一时刻事件冲突',
    'conflicting_duplicate': '重复事件内容冲突',
    'incomplete_event': '事件字段不完整',
    'baseline_established': '已建立恢复基线',
    'missing_sequence': '事件序号缺失',
    'overflow': '待补发事件过多',
    'unobserved_dispatch': '有未处理的事件',
    'unproven_event_source': '事件来源无法证明',
    'old_callback_running': '旧回调仍在运行',
    'consumer_invalidated': '消费方要求重建',
    'dependency_invalidated': '依赖模块要求重建',
    'unconfirmed': '事件处理未确认完成',
    'error': '事件处理出错',
    'cancelled': '事件处理已取消',
    'unloaded': '模块已卸载',
    'baseline_required': '需建立恢复基线',
    'pending_processing': '事件仍在处理',
    'replay_processed': '补发事件已处理',
    'unverified_transport': '连接方式无法证明连续性',
    'dependency_not_ready': '依赖模块未就绪',
    'busy': '上一轮处理未结束，本次未处理',
    'unsubscribed': '恢复订阅已取消',
}
PROGRESS = {'refreshing', 'bot_state_refreshing', 'roster_refreshing', 'verifying', 'confirming', 'persistence_pending', 'recovery_processing', 'rebuilding'}
# Framework reasons for a settled baseline; they never explain a pause.
SETTLED = {'baseline_established', 'replay_processed'}
UNKNOWN = {'', 'not_verified', 'unknown_group', 'response_unknown', 'unsupported'}
ROLES = {'owner': '群主', 'admin': '管理员', 'member': '成员'}
TITLES = {
    'overview': '群状态概览',
    'refresh': '群刷新结果',
    'roster': '群名册核验',
    'member': '成员核验结果',
    'confirm': '成员人工确认',
    'rebuild': '恢复基线重建',
}
MAX_MEMBERS = 50


def shown(names, kind, native):
    """Display text resolved by the caller (see GroupState.display_names); the raw ID when absent."""
    return (names or {}).get(kind, {}).get(native) or native


def reason_text(code):
    return REASONS.get(code, f'其他原因（{code}）') if code else ''


def label(fact):
    """Complete/verified only when the module says so; unknown is never shown as success, zero or false."""
    fact = fact if isinstance(fact, dict) else {}
    reason = fact.get('reason') or fact.get('continuity_reason') or ''
    if fact.get('complete') is True:
        return '完整'
    if fact.get('reliable') is True:
        return '可靠（已核验）'
    if reason == 'recovery_pending':
        return '暂停使用（恢复基线未完成）'
    if reason in PROGRESS:
        return f'处理中（{reason_text(reason)}）'
    if reason == 'permission_denied':
        return '无权限（平台无权限）'
    if reason.endswith('_failed') or reason == 'timeout':
        return f'失败（{reason_text(reason)}）'
    if fact.get('state') == 'unknown' or reason in UNKNOWN:
        return '未知' + (f'（{reason_text(reason)}）' if reason else '')
    if fact.get('status') in {'pending', 'observed'}:
        return f'待核验（{reason_text(reason)}）'
    return f'历史（{reason_text(reason)}）'


def paused(fact):
    # A fact with its own reason may also wait on the recovery barrier.
    fact = fact if isinstance(fact, dict) else {}
    return '；恢复基线未完成，暂不可用' if fact.get('continuity_reason') == 'recovery_pending' and fact.get('reason') else ''


def recovery_reason(recovery, pause):
    """The framework's recorded reason, unless it contradicts the current next step."""
    reason = recovery.get('reason') or ''
    if reason == 'rebuilding' and not recovery.get('rebuilding'):
        # The framework keeps the rebuild marker after a rebuild that did not establish a baseline.
        return '上次重建未完成'
    if reason in SETTLED:
        # A settled framework reason never explains a pause; name the module's own one instead.
        return reason_text(pause)
    return reason_text(reason)


def recovery_line(recovery, pause=''):
    """Processing only needs a later retry; everything else needs a rebuild.

    The module's own verdict (`recovery['pause']`, or `pause` from older snapshots) wins over the phase.
    """
    pause = recovery.get('pause', pause)
    if recovery.get('rebuilding') is True:
        return '恢复基线：未就绪（正在重建，请稍后再查看）'
    if recovery.get('ready') is True and not pause:
        return '恢复基线：已就绪'
    processing = pause in {'recovery_processing', 'persistence_pending'} if pause else recovery.get('phase') in {'processing', 'recovering'}
    state = '处理中，请稍后再查看' if processing else '需重建，请发“群状态 恢复”或在后台重建'
    reason = recovery_reason(recovery, pause)
    return f'恢复基线：未就绪（{state}' + (f'；原因：{reason}' if reason else '') + '）'


def when(value):
    return time.strftime('%m-%d %H:%M', time.localtime(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else '未查询'


def role(value):
    return ROLES.get(value, '角色未知') if isinstance(value, str) else '角色未知'


def bot_mark(value, own=False):
    return '，本 Bot' if own else {True: '，Bot', False: ''}.get(value, '，是否 Bot 未知')


def bot_state(group):
    state = group.get('bot_state')
    if not isinstance(state, dict):
        return 'Bot 在群状态未知'
    proactive = {True: '允许主动消息', False: '不允许主动消息'}.get(state.get('allow_proactive_msg'), '主动消息未知')
    prefix = '' if group.get('reliable') is True else '历史记录：'
    return f'{prefix}Bot 在群（{role(state.get("member_role"))}），{proactive}'


def group_line(group, names=None):
    members = [m for m in (group.get('members') or {}).values() if m.get('status') != 'left']
    reliable = sum(m.get('reliable') is True for m in members)
    return (
        f'{shown(names, "groups", group.get("group"))}：{label(group)}{paused(group)}；{bot_state(group)}；最近查询 {when(group.get("checked_at"))}；'
        f'名册{label(group.get("roster"))}；已知成员 {len(members)} 人，可靠 {reliable} 人'
    )


def roster_lines(result, names=None):
    complete = result.get('complete') is True
    members = result.get('members') or []
    state = '完整' if complete else '不完整，原因：' + (reason_text(result.get('reason') or result.get('continuity_reason')) or '未知')
    lines = [
        f'群 {shown(names, "groups", result.get("group"))}：名册{state}',
        f'本轮读到 {len(members)} 人（{"可作全群名单" if complete else "仅本轮观察，不能作全群名单"}）',
    ]
    for record in members[:MAX_MEMBERS]:
        lines.append(
            f'{shown(names, "members", record.get("member"))}：{role(record.get("member_role"))}{bot_mark(record.get("bot"), record.get("member") == result.get("self_member"))}'
        )
    if len(members) > MAX_MEMBERS:
        lines.append(f'另有 {len(members) - MAX_MEMBERS} 人，请在后台查看')
    if result.get('missing_known_members'):
        lines.append('名册缺少的已知成员：' + '、'.join(shown(names, 'members', m) for m in result['missing_known_members']))
    return lines


def owner_reply(operation, result, *, pause='', names=None):
    """Owner private reply; detail (members, names) never goes to group chats, see short_reply."""
    title = TITLES[operation]
    if operation == 'rebuild':
        if result.get('ready') is True:
            return f'{title}：已完成，事实恢复可用'
        # False also covers busy notifications or changes during the rebuild, not only failed verification.
        return f'{title}：未完成，事实仍暂停使用。请查看“群状态”中未就绪或待核验的群／成员，处理后再发“群状态 恢复”'
    if operation == 'overview':
        groups = result.get('groups') or []
        lines = [recovery_line(result.get('recovery') or {}, pause), f'已知群 {len(groups)} 个', *(group_line(g, names) for g in groups)]
        return '\n'.join([f'{title}（仅已知群，不代表全部群）', *lines])
    if operation == 'refresh':
        lines = [
            f'群 {shown(names, "groups", result.get("group"))}：{label(result)}{paused(result)}',
            bot_state(result),
            f'最近查询 {when(result.get("checked_at"))}',
        ]
    elif operation == 'roster':
        lines = roster_lines(result, names)
    else:
        member = (
            f'{shown(names, "members", result.get("member"))}：{label(result)}{paused(result)}，{role(result.get("member_role"))}{bot_mark(result.get("bot"))}'
        )
        lines = [f'群 {shown(names, "groups", result.get("group"))} 成员 {member}', f'最近查询 {when(result.get("checked_at"))}']
    return '\n'.join([title, *lines])


def short_reply(operation):
    return f'{TITLES[operation]}已处理，详情请在主人私聊或已登录后台查看'
