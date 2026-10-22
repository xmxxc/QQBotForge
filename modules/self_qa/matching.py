"""Namespace-isolated exact/contains matching; consumers own admission and effects.

The ``public`` namespace belongs to the module's own message entry (entry.py).
"""

import asyncio
from uuid import uuid4

from .storage import ExtensionStore

FIELDS = {'name', 'kind', 'target', 'keywords', 'mode', 'priority', 'enabled', 'groups'}


def namespace_key(namespace):
    if not isinstance(namespace, str) or not namespace or len(namespace) > 100:
        raise ValueError('请选择明确的消费方命名空间')
    return 'qa_rules:' + namespace


def native_ids(values, *, maximum=500):
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError(f'群原生ID须为最多 {maximum} 项的列表')
    if not all(isinstance(v, str) and v.strip() and len(v) <= 256 and v == v.strip() for v in values):
        raise ValueError('群原生ID须为非空文字，最多 256 字符')
    return list(dict.fromkeys(values))


def validate_settings(settings):
    if not isinstance(settings, dict) or set(settings) - {'groups', 'private'}:
        raise ValueError('启用范围只接受 groups、private')
    if not isinstance(settings.get('private', False), bool):
        raise ValueError('private 须为布尔值')
    return {'groups': native_ids(settings.get('groups', [])), 'private': settings.get('private', False)}


def validate_rule(rule):
    # Empty groups is a global rule; otherwise only the listed native groups.
    rule['groups'] = native_ids(rule.get('groups', []))
    if rule.get('kind') not in {'text', 'resource'} or rule.get('mode') not in {'exact', 'contains'}:
        raise ValueError('规则类型或匹配方式无效')
    if not isinstance(rule.get('enabled'), bool):
        raise ValueError('规则启用状态须为布尔值')
    for key in ('name', 'target'):
        if not isinstance(rule.get(key), str) or not rule[key].strip() or len(rule[key]) > 4000:
            raise ValueError('规则名称与目标须为非空文字，最多 4000 字符')
    priority = rule.get('priority')
    if priority is not None and (type(priority) is not int or not -100000 <= priority <= 100000):
        raise ValueError('优先级须为 -100000 至 100000 的整数或空值')
    keywords = rule.get('keywords')
    if not isinstance(keywords, list) or not 1 <= len(keywords) <= 50:
        raise ValueError('请提供 1 至 50 个关键词')
    normalized = []
    for keyword in keywords:
        entry = {'text': keyword, 'enabled': True} if isinstance(keyword, str) else keyword
        if (
            not isinstance(entry, dict)
            or set(entry) - {'text', 'enabled'}
            or not isinstance(entry.get('text'), str)
            or not entry['text'].strip()
            or len(entry['text']) > 500
            or not isinstance(entry.get('enabled', True), bool)
        ):
            raise ValueError('关键词须为非空文字或 text/enabled 对象，最多 500 字符')
        normalized.append({'text': entry['text'].strip(), 'enabled': entry.get('enabled', True)})
    rule['keywords'] = normalized
    return rule


class QuestionAnswer:
    def __init__(self, path):
        self.store = ExtensionStore(path)
        self.lock = asyncio.Lock()
        self.active = True
        self.versions = {}

    async def initialize(self):
        await self.store.ready()

    def close(self):
        self.active = False
        self.store.close()

    def version(self, bot, namespace):
        return self.versions.get((bot, namespace), 0)

    async def manage(self, bot, namespace, operation, data=None, key=''):
        if not self.active:
            raise RuntimeError('自助问答模块未启用')
        collection = namespace_key(namespace)
        async with self.lock:
            if operation == 'list':
                return await self.store.all(bot, collection)
            old = await self.store.get(bot, collection, key) if key else None
            if operation == 'get':
                return old
            if operation in {'update', 'delete'} and not old:
                raise KeyError('规则不存在')
            if operation == 'delete':
                self.versions[(bot, namespace)] = self.version(bot, namespace) + 1
                await self.store.delete(bot, collection, key)
                return {'id': key}
            if operation not in {'create', 'update'} or not isinstance(data, dict) or set(data) - FIELDS:
                raise ValueError('规则操作或字段无效')
            rule = old or {'id': uuid4().hex, 'enabled': True, 'mode': 'exact', 'kind': 'resource', 'priority': None}
            rule.update(data)
            validate_rule(rule)
            self.versions[(bot, namespace)] = self.version(bot, namespace) + 1
            await self.store.put(bot, collection, rule['id'], rule)
            return rule

    async def settings(self, bot, namespace):
        """Owner-chosen enablement; defaults to no group and no private chat."""
        namespace_key(namespace)
        saved = await self.store.get(bot, 'qa_settings', namespace)
        return saved or {'groups': [], 'private': False}

    async def save_settings(self, bot, namespace, settings):
        if not self.active:
            raise RuntimeError('自助问答模块未启用')
        namespace_key(namespace)
        settings = validate_settings(settings)
        async with self.lock:
            await self.store.put(bot, 'qa_settings', namespace, settings)
            self.versions[(bot, namespace)] = self.version(bot, namespace) + 1
        return settings

    async def match(self, bot, namespace, text, *, group=None):
        """group=None skips rule scope (consumer-owned); '' is private chat, which only global rules serve."""
        if not isinstance(text, str):
            return []
        text = text.strip()
        results = []
        for rule in await self.manage(bot, namespace, 'list'):
            if not rule['enabled'] or group is not None and rule.get('groups') and group not in rule['groups']:
                continue
            hits = [k['text'] for k in rule['keywords'] if k['enabled'] and (text == k['text'] if rule['mode'] == 'exact' else k['text'] in text)]
            if hits:
                # Explicit equal priorities remain ambiguous; unset priorities use keyword length.
                rank = (1, 0, 0) if rule['mode'] == 'exact' else (0, rule['priority'] or 0, max(map(len, hits)) if rule['priority'] is None else 0)
                results.append(
                    {'rule_id': rule['id'], 'name': rule['name'], 'kind': rule['kind'], 'target': rule['target'], 'mode': rule['mode'], 'rank': rank}
                )
        return sorted(results, key=lambda r: r['rank'], reverse=True)

    @staticmethod
    def best(results):
        if not results:
            return []
        rank = max(r['rank'] for r in results)
        return [r for r in results if r['rank'] == rank]

    async def answer(self, bot, namespace, text, send, *, allowed=False, group=None):
        """Opt-in static reply adapter. Never registers an event listener or executes actions."""
        if allowed is not True:
            return []
        targets = dict.fromkeys(r['target'] for r in self.best(await self.match(bot, namespace, text, group=group)) if r['kind'] == 'text')
        return [await send(target) for target in targets]
