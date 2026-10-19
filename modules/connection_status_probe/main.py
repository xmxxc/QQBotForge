"""PR #110 的临时真机观察模块；动作只由本机控制文件触发。"""

import asyncio
import json
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from core.application import get_app

__module_meta__ = {
    'name': '连接状态真机观察',
    'version': '1.0.0',
    'description': '记录连接事实、查询结果和事件上下文；支持按 AppID 单次断开 WS。',
}

_instance = None


def safe_context(context):
    if context is None:
        return None
    value = asdict(context)
    value.pop('session_id', None)
    return value


def safe_status(status):
    value = asdict(status)
    value['context'] = safe_context(status.context)
    return value


class Probe:
    def __init__(self, ctx):
        self.ctx = ctx
        self.run = uuid4().hex[:8]
        self.data = Path(ctx.data_dir)
        self.task = None
        self.subscription = None
        self.group_subscription = None

    def emit(self, kind, **values):
        record = {
            'time': datetime.now(UTC).isoformat(),
            'run': self.run,
            'kind': kind,
            **values,
        }
        line = json.dumps(record, ensure_ascii=False)
        with (self.data / 'status.jsonl').open('a', encoding='utf-8') as output:
            output.write(line + '\n')
        self.ctx.log.info('[connection-probe] ' + line)

    async def on_status(self, status):
        self.emit('status', subscription_id=self.subscription.subscription_id, **safe_status(status))

    async def on_group(self, event):
        self.emit('group_event', event_type=event.event_type, context=safe_context(event.connection_context))

    def record_event(self, event, tag=None):
        self.emit('message_event', tag=tag, event_type=event.event_type, context=safe_context(event.connection_context))

    def snapshot(self):
        result = {
            'run': self.run,
            'states': [safe_status(status) for status in self.ctx.connection_statuses],
            'results': [
                {'revision': item.status.revision, 'outcome': item.outcome, 'error_type': item.error_type}
                for item in self.subscription.results[-20:]
            ],
        }
        (self.data / 'snapshot.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.emit('snapshot', **{key: value for key, value in result.items() if key != 'run'})
        return result

    def handle(self, request):
        action = request.get('action')
        if action == 'snapshot':
            return self.snapshot()
        if action != 'drop-ws':
            raise ValueError('只支持 snapshot 或 drop-ws')
        appid = str(request.get('appid', '')).strip()
        if not appid:
            raise ValueError('drop-ws 必须指定 AppID')
        app = get_app()
        bot = app.get_bot(appid) if app is not None else None
        client = getattr(bot, 'ws_client', None)
        socket = getattr(client, '_ws', None)
        transport = getattr(socket, 'transport', None)
        if transport is None:
            raise RuntimeError('该 AppID 当前没有可断开的 WS 连接')
        self.emit('drop_requested', appid=appid)
        # 仅模拟此连接的 TCP 中断，不调用 WSClient.close()，保留实际 Resume 参数。
        transport.abort()
        return {'appid': appid, 'action': action, 'run': self.run}

    async def control_loop(self):
        request_path = self.data / 'request.json'
        while True:
            if request_path.is_file():
                request = {}
                try:
                    value = json.loads(request_path.read_text(encoding='utf-8'))
                    if not isinstance(value, dict):
                        raise ValueError('控制请求必须是 JSON 对象')
                    request = value
                    request_path.unlink()
                    expires_at = request.get('expires_at')
                    if type(expires_at) not in (int, float) or not time.time() < expires_at <= time.time() + 15:
                        raise ValueError('控制请求已过期或有效期不合法，请重新执行命令')
                    result = {'ok': True, 'result': self.handle(request)}
                except Exception as error:
                    request_path.unlink(missing_ok=True)
                    result = {'ok': False, 'error': type(error).__name__, 'message': str(error)}
                response = {'id': request.get('id'), **result}
                pending = self.data / 'response.tmp'
                pending.write_text(json.dumps(response, ensure_ascii=False), encoding='utf-8')
                pending.replace(self.data / 'response.json')
            await asyncio.sleep(0.2)

    def start(self):
        if getattr(self.ctx, 'connection_status_api_version', None) != 1:
            raise RuntimeError('当前框架不含连接状态 API v1，请确认运行的是 PR #110 代码')
        # 不在重新启用后执行上一次停机时残留的断线请求。
        (self.data / 'request.json').unlink(missing_ok=True)
        self.subscription = self.ctx.subscribe_connection_status(self.on_status)
        self.group_subscription = self.ctx.subscribe_group_lifecycle(self.on_group)
        self.task = asyncio.create_task(self.control_loop())
        self.emit('loaded', subscription_id=self.subscription.subscription_id)

    async def close(self):
        self.subscription.cancel()
        self.group_subscription.cancel()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        self.emit('unloaded', subscription_active=self.subscription.active)


def setup(ctx):
    global _instance
    _instance = Probe(ctx)
    _instance.start()
    return _instance


async def teardown():
    global _instance
    if _instance is not None:
        await _instance.close()
        _instance = None
