"""Chinese owner summaries; the authenticated page and HTTP keep the full records."""

import time

KINDS = {
    'grant': '授权',
    'bulk_grant': '批量授权',
    'bulk_revoke': '批量撤权',
    'bulk_expiry': '批量期限',
    'migration': '旧数据迁移',
    'delivery': '账户发送',
    'group_mode': '群发设置',
    'group_recovered': '群发恢复',
}
TASKS = {
    'legacy_adoption': '旧授权待恢复',
    'verification': '成员待核验',
    'binding': '待身份绑定',
    'binding_request': '身份绑定申请待处理',
    'group_delivery': '群发未生效',
    'notification': '主人通知未送达',
}
# Each field names its own outcome; 'not_requested' is "not executed", never success.
STATUS = {
    'saved': '已保存',
    'partial': '部分成功',
    'failed': '失败',
    'imported': '已导入',
    'not_requested': '未执行',
    'pending': '待处理',
    'pending_binding': '待身份绑定',
    'verified': '已绑定',
    'sent': '已发送',
    'unknown': '结果未知',
    'unavailable': '无法执行',
}
FIELDS = (('authorization', '授权'), ('binding', '绑定'), ('mode', '模式'), ('delivery', '发送'), ('notification', '通知'))
MODES = {'group': '群发', 'private': '私发', 'exact': '精确匹配', 'contains': '包含匹配', 'regex': '正则匹配'}
LIMIT = 20


def status(value):
    return STATUS.get(value, MODES.get(value, f'其他（{value}）'))


def named(table, code, noun):
    return table.get(code) or (f'其他{noun}（{code}）' if code else f'未知{noun}')


def operation_line(result):
    parts = []
    for key, name in FIELDS:
        if key in result:
            text = name + status(result[key])
            summary = result.get('summary') if key == 'authorization' else None
            if isinstance(summary, dict):
                text += f'（成功 {summary.get("success", "未知")}，失败 {summary.get("failed", "未知")}）'
            parts.append(text)
    if result.get('mode_requested') and result.get('mode_requested') != result.get('mode'):
        parts.append(f'请求{status(result["mode_requested"])}未生效')
    # Raw codes from other modules: shown as-is rather than guessed into Chinese.
    parts += [f'{name} {result[key]}' for key, name in (('reason', '原因'), ('error', '错误码')) if result.get(key)]
    when = time.strftime('%m-%d %H:%M', time.localtime(result['created_at'])) if isinstance(result.get('created_at'), (int, float)) else '时间未知'
    scope = '，'.join(f'{name} {result[key]}' for key, name in (('group', '群'), ('member', '成员')) if result.get(key))
    return f'{when} {named(KINDS, result.get("kind"), "操作")} {str(result.get("id", ""))[:8]}{"（" + scope + "）" if scope else ""}：{"；".join(parts) or "无状态"}'


def results(rows):
    if not rows:
        return '账户管理结果：暂无'
    rows = sorted(rows, key=lambda r: r.get('created_at') or 0, reverse=True)
    lines = [f'账户管理结果（共 {len(rows)} 条，最新在前）', *map(operation_line, rows[:LIMIT])]
    if len(rows) > LIMIT:
        lines.append(f'另有 {len(rows) - LIMIT} 条，请在后台查看')
    return '\n'.join(lines)


def tasks(rows):
    if not rows:
        return '账户管理待办：暂无'
    lines = [f'账户管理待办（共 {len(rows)} 项）']
    for index, task in enumerate(rows[:LIMIT], 1):
        name = named(TASKS, task.get('type'), '待办')
        if task.get('type') == 'notification':
            lines.append(f'{index}. {name}：{operation_line(task)}；处理：账户管理 重试通知 {task.get("operation_id")}')
            continue
        scope = ' '.join(f'{label} {task[key]}' for key, label in (('group', '群'), ('member', '成员'), ('resource', '账户')) if task.get(key))
        lines.append(f'{index}. {name}：{scope}；处理：{task.get("action")}')
    if len(rows) > LIMIT:
        lines.append(f'另有 {len(rows) - LIMIT} 项，请在后台查看')
    return '\n'.join(lines)


def rule_line(rule, names):
    keywords = [k.get('text') if isinstance(k, dict) else k for k in rule.get('keywords') or []]
    # Text answers may hold anything the owner typed; only the target account's name is shown.
    target = f'指向账户 {names.get(rule.get("target"), "（账户不存在）")}' if rule.get('kind', 'resource') == 'resource' else '文本回复'
    return f'{rule.get("name") or rule.get("id")}：{"启用" if rule.get("enabled") else "停用"}；{MODES.get(rule.get("mode"), rule.get("mode"))}：{"、".join(map(str, keywords))}；{target}'


def rules(result, names):
    if isinstance(result, list):
        if not result:
            return '账户规则：暂无'
        return '\n'.join([f'账户规则（共 {len(result)} 条）', *(f'{rule_line(r, names)}（{r.get("id")}）' for r in result)])
    if set(result) == {'id'}:
        return f'账户规则已删除：{result["id"]}'
    return f'账户规则已保存：{rule_line(result, names)}（{result.get("id")}）'
