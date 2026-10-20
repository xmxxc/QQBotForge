"""Namespace-isolated exact/contains matching; consumers own admission and effects.

The ``public`` namespace belongs to the module's own message entry (entry.py).
"""

import asyncio
from uuid import uuid4

from .regex_runner import RegexRunner, compile_check
from .storage import ExtensionStore

# Exact beats contains beats regex; within a mode a higher priority wins.
MODES = {'exact': 2, 'contains': 1, 'regex': 0}
FIELDS = {'name', 'kind', 'target', 'keywords', 'mode', 'priority', 'enabled', 'groups', 'access', 'function'}
ACCESS = {'public', 'restricted'}


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
    # Restricted rules are decided by the authorization module; public is the default.
    rule.setdefault('access', 'public')
    if rule['access'] not in ACCESS:
        raise ValueError('访问方式须为 public 或 restricted')
    function = rule.get('function')
    if function is not None and (not isinstance(function, str) or not function or len(function) > 256):
        raise ValueError('规则授权功能须为非空文字或空值')
    if rule.get('kind') not in {'text', 'resource'} or rule.get('mode') not in MODES:
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
        if rule['mode'] == 'regex':
            compile_check(normalized[-1]['text'])
    rule['keywords'] = normalized
    return rule


class QuestionAnswer:
    def __init__(self, path):
        self.store = ExtensionStore(path)
        self.lock = asyncio.Lock()
        self.active = True
        self.versions = {}
        self.regex = RegexRunner()

    async def initialize(self):
        await self.store.ready()

    def close(self):
        self.active = False
        self.regex.close()
        self.store.close()

    def version(self, bot, namespace):
        return self.versions.get((bot, namespace), 0)

    async def manage(self, bot, namespace, operation, data=None, key='', *, guard=None, scope=None):
        """Rule CRUD. ``guard()`` is re-checked under the lock right before a write;
        ``scope(old_rule)`` limits which existing rules a caller may touch."""
        if not self.active:
            raise RuntimeError('自助问答模块未启用')
        collection = namespace_key(namespace)
        async with self.lock:
            if operation == 'list':
                return await self.store.all(bot, collection)
            old = await self.store.get(bot, collection, key) if key else None
            if operation == 'get':
                return old
            if operation in {'update', 'delete'} and (not old or scope is not None and not scope(old)):
                raise KeyError('规则不存在')
            if operation in {'create', 'update', 'delete'} and guard is not None and not guard():
                raise PermissionError('管理资格已变化，请重新发送')
            if operation == 'delete':
                self.versions[(bot, namespace)] = self.version(bot, namespace) + 1
                await self.store.delete(bot, collection, key)
                return {'id': key}
            if operation not in {'create', 'update'} or not isinstance(data, dict) or set(data) - FIELDS:
                raise ValueError('规则操作或字段无效')
            rule = old or {'id': uuid4().hex, 'enabled': True, 'mode': 'exact', 'kind': 'resource', 'priority': None}
            rule.update(data)
            # A rejected rule raises before the store write; the saved rule stays as it was.
            validate_rule(rule)
            rule['revision'] = rule.get('revision', 0) + 1
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

    async def match(self, bot, namespace, text, *, group=None, admit=None):
        """Every admitted rule that matches, best first, one result per rule.

        group=None skips rule scope (consumer-owned); '' is private chat, which
        only global rules serve. ``admit(rule)`` (sync or async) runs on every
        hit before ranking, so ties never expose rules the caller may not see. Regex rules
        run bounded in the Bot's worker; a timeout leaves them unmatched.
        """
        if not isinstance(text, str):
            return []
        text = text.strip()
        rules = [
            rule
            for rule in await self.manage(bot, namespace, 'list')
            if rule['enabled'] and (group is None or not rule.get('groups') or group in rule['groups'])
        ]
        patterns = list(dict.fromkeys(k['text'] for rule in rules if rule['mode'] == 'regex' for k in rule['keywords'] if k['enabled']))
        found = dict(zip(patterns, await self.regex.search(bot, patterns, text) or [False] * len(patterns), strict=True))
        results = []
        for rule in rules:
            keywords = [k['text'] for k in rule['keywords'] if k['enabled']]
            if rule['mode'] == 'exact':
                hits = [k for k in keywords if text == k]
            elif rule['mode'] == 'contains':
                hits = [k for k in keywords if k in text]
            else:
                hits = [k for k in keywords if found.get(k)]
            if not hits:
                continue
            if admit is not None:
                allowed = admit(rule)
                if not (await allowed if asyncio.iscoroutine(allowed) else allowed):
                    continue
            # Explicit equal priorities stay tied; unset contains priority prefers the longer keyword.
            length = max(map(len, hits)) if rule['mode'] == 'contains' and rule['priority'] is None else 0
            results.append(
                {
                    'rule_id': rule['id'],
                    'revision': rule.get('revision', 0),
                    'name': rule['name'],
                    'kind': rule['kind'],
                    'target': rule['target'],
                    'mode': rule['mode'],
                    'rank': (MODES[rule['mode']], rule['priority'] or 0, length),
                }
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
