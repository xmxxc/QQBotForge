"""在测试部署根目录执行；通过本地文件向已启用的观察模块提交一个动作。"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4


def identity(record):
    context = record.get('context') or {}
    fields = ('appid', 'transport', 'runtime_id', 'bot_generation', 'connection_generation', 'session_generation')
    value = tuple(context.get(field) for field in fields)
    return value if all(isinstance(item, str) and item for item in value) and value[1] == 'websocket' else None


def summarize(records, appid=None):
    appids = sorted({(row.get('context') or {}).get('appid') for row in records} - {None, ''})
    if appid is not None:
        appids = [appid]
    reports = []
    for bot in appids:
        rows = [row for row in records if (row.get('context') or {}).get('appid') == bot]
        statuses = [row for row in rows if row.get('kind') == 'status' and identity(row)]
        ready = [row for row in statuses if row.get('phase') in ('ready', 'replay_complete')]
        checks = dict.fromkeys(('first', 'resume', 'new_session', 'message_context', 'restart', 'reload'), False)
        checks['first'] = any(row.get('phase') == 'ready' and row.get('login_kind') == 'first' and row.get('continuity') == 'unknown' for row in ready)
        runtimes = {identity(row)[2] for row in ready if row.get('phase') == 'ready' and row.get('login_kind') == 'first' and row.get('continuity') == 'unknown'}
        checks['restart'] = len(runtimes) > 1
        for index, row in enumerate(statuses):
            key = identity(row)
            previous = statuses[:index]
            prior_ready = [old for old in previous if old.get('phase') in ('ready', 'replay_complete')]
            if row.get('phase') == 'ready' and row.get('login_kind') == 'new' and row.get('continuity') == 'unknown':
                checks['new_session'] |= any(identity(old)[:4] == key[:4] and identity(old)[5] != key[5] and identity(old)[4] != key[4] for old in prior_ready)
            if row.get('phase') == 'replay_complete' and row.get('continuity') == 'resumed' and row.get('login_kind') == 'resume':
                attempt = row['context'].get('resume_attempt_id')
                start, end, receive_end = (row.get(field) for field in ('resume_from_sequence', 'replay_end_sequence', 'replay_end_receive_index'))
                boundary = all(type(value) is int and value >= 0 for value in (start, end, receive_end)) and end >= start
                capability = row.get('capabilities') or {}
                origin = any(identity(old)[:4] == key[:4] and identity(old)[5] == key[5] and identity(old)[4] != key[4] for old in prior_ready)
                resuming = any(identity(old) == key and old.get('phase') == 'resuming' and old.get('continuity') == 'pending' and old['context'].get('resume_attempt_id') == attempt and old.get('resume_from_sequence') == start for old in previous)
                checks['resume'] |= bool(attempt and boundary and capability.get('session_resume') is True and capability.get('replay_boundary') is True and origin and resuming)
            if row.get('is_snapshot') is True:
                unloaded = {old.get('run') for old in records if old.get('kind') == 'unloaded' and old.get('subscription_active') is False}
                checks['reload'] |= any(identity(old) == key and old.get('run') != row.get('run') and old.get('run') in unloaded and old.get('subscription_id') != row.get('subscription_id') for old in previous)
        messages = [row for row in rows if row.get('kind') == 'message_event']
        checks['message_context'] = any(identity(row) and any(identity(status) == identity(row) for status in statuses) for row in messages)
        reports.append({'appid': bot, 'checks': checks, 'status_count': len(statuses), 'message_count': len(messages)})
    return reports


def report(data, appid):
    path = data / 'status.jsonl'
    records = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get('context', {}), (dict, type(None))):
            raise ValueError(f'第 {number} 行不是观察模块的记录')
        records.append(value)
    reports = summarize(records, appid)
    labels = {'first': '首次就绪', 'resume': '同会话恢复及补发边界', 'new_session': '建立新会话', 'message_context': '消息连接上下文', 'restart': '进程重启后更换代际', 'reload': '模块重载及当前快照'}
    for item in reports:
        print(f"AppID {item['appid']}（状态 {item['status_count']} 条，验收消息 {item['message_count']} 条）")
        for name, observed in item['checks'].items():
            print(f"  {labels[name]}：{'已观测' if observed else '未覆盖'}")
    if not reports:
        print('尚未记录到 Bot 状态，请先启用观察模块并连接测试机器人。')
    print('汇总仅表示日志已记录对应事实；未覆盖项不自动判为失败，请结合操作步骤核对。')
    (data / 'report.json').write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['snapshot', 'drop-ws', 'report'])
    parser.add_argument('appid', nargs='?')
    args = parser.parse_args()
    if args.action == 'drop-ws' and not args.appid:
        parser.error('drop-ws 必须指定 AppID')
    data = Path(__file__).resolve().parent / 'modules/connection_status_probe/data'
    if not data.is_dir():
        parser.error('请先在框架模块管理中启用“连接状态真机观察”')
    if args.action == 'report':
        try:
            return report(data, args.appid)
        except (OSError, ValueError) as error:
            parser.error(f'无法读取观察记录：{error}')
    target = data / 'request.json'
    if target.exists():
        parser.error('前一个命令尚未处理，请确认模块仍在运行')
    request_id = uuid4().hex
    pending = data / f'request-{request_id}.tmp'
    pending.write_text(json.dumps({'id': request_id, 'action': args.action, 'appid': args.appid, 'expires_at': time.time() + 10}), encoding='utf-8')
    pending.replace(target)
    deadline = time.monotonic() + 10
    response = data / 'response.json'
    while time.monotonic() < deadline:
        try:
            value = json.loads(response.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = {}
        if isinstance(value, dict) and value.get('id') == request_id:
            print(json.dumps(value, ensure_ascii=False, indent=2))
            return 0 if value.get('ok') else 1
        time.sleep(0.1)
    print('10秒内未收到确认；请检查模块日志。请求可能仍在队列中，确认前不要重发。')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
