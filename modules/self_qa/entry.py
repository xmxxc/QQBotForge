"""Public question/answer message entry and owner management for the ``public`` namespace.

Messages arrive through the framework ``before_handler`` hook, which runs after
message logging and the dispatcher's receive checks (blacklist, maintenance,
non-@ permission, interceptors). Not enabled or not matched messages pass on
to plugin handlers untouched; a matched answer consumes the message.
"""

import json
import sqlite3

from aiohttp import web

from core.base.config import cfg
from core.plugin._blacklist import get_blacklist_map
from core.plugin._dispatch import _strip_leading_bot_name_at

from .access import AccessBridge
from .delivery import TEXT, Delivery, EventTarget, UserTarget, outcome
from .menus import PAGE, Menus, context_of

PREFIX = '/api/ext/self-qa/'
NAMESPACE = 'public'
COMMAND = '问答管理 '
MESSAGE_TYPES = {'GROUP_AT_MESSAGE_CREATE', 'GROUP_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE'}
SELECT = '问答选择'
CHOOSE_GROUP = '问答群'
RESTRICTED_TITLE = '受限答案'
CALLBACK = 'qa:'
# Rules changed while an answer was prepared: re-resolve from current rules this often.
ATTEMPTS = 3
CHANGING = '答案正在更新，请稍后重新提问。'
MANAGER_CHANGED = '管理资格已变化，请重新发送'
STALE = object()


def owner(event):
    return bool(event.user_id) and event.user_id in ((cfg.get_bot_config(event.appid) or {}).get('owner_ids') or [])


class PublicEntry:
    def __init__(self, qa):
        self.qa = qa
        self.menus = Menus()
        self.access = AccessBridge(qa, NAMESPACE)
        self.delivery = Delivery(self)
        # Private chat: the group context an asker chose among several valid ones (re-validated on every use).
        self.private_groups = {}

    @staticmethod
    def sender(bot):
        from core.application import get_app

        app = get_app()
        instance = app.get_bot(bot) if app else None
        return instance.sender if instance else None

    async def execute(self, bot, data):
        if not isinstance(data, dict):
            raise ValueError('操作须为对象')
        values = dict(data)
        operation = values.pop('operation', '')
        if operation == 'list':
            if values:
                raise ValueError('查看不接受其他字段')
            return {
                'settings': await self.qa.settings(bot, NAMESPACE),
                'rules': await self.qa.manage(bot, NAMESPACE, 'list'),
                'capabilities': {**await self.access.status(bot), 'formats': self.delivery.capabilities(bot)},
            }
        if operation == 'settings':
            return await self.qa.save_settings(bot, NAMESPACE, values)
        if 'function' in values:
            raise ValueError('规则授权功能请用 rule_policy 设置')
        if operation == 'delegate':
            # Owner only: open delegation per group; role is decided by authorization.
            if set(values) != {'groups'}:
                raise ValueError('委托须提供 groups 列表')
            return {'delegated_groups': await self.access.set_delegation(bot, values['groups'])}
        if operation == 'rule_policy':
            if set(values) != {'id', 'policy'}:
                raise ValueError('须提供规则 id 与 policy（null 为继承默认受限策略）')
            rule = await self.qa.manage(bot, NAMESPACE, 'get', key=values['id'])
            if not rule or rule.get('access') != 'restricted':
                raise ValueError('只有受限规则可设置单独策略')
            function = await self.access.set_rule_policy(bot, rule, values['policy'])
            return await self.qa.manage(bot, NAMESPACE, 'update', {'function': function}, values['id'])
        if operation == 'create':
            return await self.qa.manage(bot, NAMESPACE, 'create', {'kind': 'text', **values})
        if operation == 'test':
            # Owner feedback: what a message would match, without sending.
            if set(values) - {'text', 'group'} or not isinstance(values.get('text'), str):
                raise ValueError('测试须提供 text，可选 group')
            results = await self.qa.match(bot, NAMESPACE, values['text'], group=values.get('group', ''))
            return {'matches': results, 'best': [r['rule_id'] for r in self.candidates(results)]}
        if operation in {'update', 'delete'}:
            key = values.pop('id', '')
            if not isinstance(key, str) or not key:
                raise ValueError('请提供规则 id')
            return await self.qa.manage(bot, NAMESPACE, operation, values if operation == 'update' else None, key)
        raise ValueError('操作须为 list/settings/create/update/delete/test/delegate/rule_policy')

    async def reply(self, event, content, **kwargs):
        # before_event runs before dispatch assigns event._sender; use the Bot's sender.
        sender = self.sender(event.appid)
        return await sender.reply(event, content, **kwargs) if sender else None

    async def enabled(self, event):
        settings = await self.qa.settings(event.appid, NAMESPACE)
        return event.group_id in settings['groups'] if event.is_group else settings['private']

    def candidates(self, results):
        """Best tied text answers, one per distinct answer."""
        chosen = {}
        for result in self.qa.best([r for r in results if r['kind'] == 'text']):
            chosen.setdefault(result['target'], result)
        return list(chosen.values())

    async def matched(self, event, text):
        """Best candidates this asker may see, the rules and decisions behind them, and group choices."""
        if not await self.enabled(event):
            return [], {}, []
        if event.is_group:
            candidates, admitted, _ = await self.match_in(event, text, None, {})
            return candidates, admitted, []
        contexts, context = await self.private_scope(event)
        candidates, admitted, choices = await self.match_in(event, text, context, contexts)
        if candidates or context or len(contexts) < 2:
            return candidates, admitted, choices
        # Several valid groups, none chosen: offer only the groups where this asker has an answer.
        for group, option in contexts.items():
            if group not in choices and (await self.match_in(event, text, option, contexts))[0]:
                choices.append(group)
        return [], {}, choices

    async def private_scope(self, event):
        """Private chat: valid group contexts (Q&A enabled groups) and the one in use, if any."""
        bot, member = event.appid, event.raw_user_id or event.user_id
        settings = await self.qa.settings(bot, NAMESPACE)
        contexts = await self.access.group_contexts(bot, member, settings['groups'])
        chosen = self.private_groups.get((bot, member), '')
        if chosen not in contexts:
            # A stale choice is dropped; a single valid group needs no choice.
            chosen = next(iter(contexts)) if len(contexts) == 1 else ''
        return contexts, contexts.get(chosen)

    async def match_in(self, event, text, context, contexts):
        """Candidates in the event's group, or in private chat with ``context`` (or none)."""
        group, member = (event.group_id if event.is_group else ''), event.raw_user_id or event.user_id
        admitted, choices = {}, []

        async def admit(rule):
            decision = await self.access.admit(event.appid, rule, group, member, context=context, groups=list(contexts))
            if decision is not None and decision.get('choose'):
                choices.extend(g for g in decision['choose'] if g not in choices)
                return False
            if decision is not None:
                admitted[rule['id']] = rule, decision
            return decision is not None

        rules_group = group or (context['group'] if context else '')
        return self.candidates(await self.qa.match(event.appid, NAMESPACE, text, group=rules_group, admit=admit)), admitted, choices

    def fresh(self, bot, version):
        """Final non-awaiting check: no rule or enablement changed since ``version`` was read."""
        return self.qa.active and self.qa.version(bot, NAMESPACE) == version

    async def resolved(self, event, run):
        """``run(version)`` returns STALE when rules changed before anything was sent; then run again on current rules."""
        for _ in range(ATTEMPTS):
            result = await run(self.qa.version(event.appid, NAMESPACE))
            if result is not STALE:
                return result
        await self.reply(event, CHANGING)
        return None

    async def current(self, event, menu):
        candidates, admitted, _ = await self.matched(event, menu['text'])
        return candidates, admitted

    async def titles(self, event, candidates, admitted):
        """Restricted titles stay hidden in a group whose members may not all view them.

        Returns the titles and the (rule, report) pairs behind each shown restricted title.
        """
        names, shown = [], []
        for candidate in candidates:
            rule = admitted[candidate['rule_id']][0]
            report = await self.access.group_view(event.appid, rule, event.group_id) if rule.get('access') == 'restricted' and event.is_group else None
            if report is not None:
                shown.append((rule, report))
            hidden = rule.get('access') == 'restricted' and event.is_group and report is None
            names.append(RESTRICTED_TITLE if hidden else candidate['name'])
        return names, shown

    async def deliver(self, event, candidate, admitted, version):
        """Send one matched answer; STALE when its rules changed during the waits (nothing sent)."""
        rule, decision = admitted[candidate['rule_id']]
        report = None
        if rule.get('access') == 'restricted' and event.is_group:
            report = await self.access.group_view(event.appid, rule, event.group_id)
        if rule.get('access') == 'restricted' and event.is_group and report is None:
            if not self.fresh(event.appid, version):
                return STALE
            return await self.deliver_privately(event, rule, decision, version)

        def check():
            """Final non-awaiting gate before every part: rule version, asker, whole-group report."""
            return (
                self.fresh(event.appid, version)
                and self.access.current(event.appid, decision)
                and (report is None or self.access.report_current(event.appid, rule, event.group_id, report))
            )

        state, _ = await self.send(EventTarget(self, event, context_of(event)), rule, check)
        if state == 'stale':
            if not self.fresh(event.appid, version):
                return STALE
            await self.reply(event, '资格已变化，请重新提问。')
        elif state == 'partial':
            # Parts already sent stay sent; the rest is withheld.
            await self.reply(event, '答案已变化，其余内容未发送，请重新提问。')
        return None

    async def send(self, target, rule, check):
        buttons = rule.get('buttons', [])
        token = (
            self.menus.issue_links(target.context, NAMESPACE, [b['value'] if b['action'] == 'rule' else None for b in buttons])
            if any(b['action'] == 'rule' for b in buttons)
            else ''
        )
        return await self.delivery.send(target, rule, token, check)

    async def deliver_privately(self, event, rule, decision, version):
        """Not every group member may view: status in the group, answer to the asker's verified private identity."""
        bot, group, member = event.appid, event.group_id, event.raw_user_id or event.user_id
        found = await self.access.member_context(bot, rule, group, member)
        if not self.fresh(bot, version):
            return STALE
        context = found['context']
        if context is None:
            if found['reason'] == 'binding_missing':
                await self.reply(event, f'该答案仅限有权成员查看，不能在本群发送。请先私聊 Bot 发送：身份绑定 申请 {group} {member}，主人核实后即可私聊接收。')
            else:
                await self.reply(event, '该答案仅限有权成员查看，暂不能私聊发送，请联系主人。')
            return None

        def check():
            """Final non-awaiting gate before every private part: rule version, asker, recipient identity."""
            return self.fresh(bot, version) and self.access.current(bot, decision) and self.access.context_current(bot, context)

        sender = self.sender(bot)
        if sender is None:
            await self.reply(event, '资格已变化，请重新提问。')
            return None
        state, error = await self.send(UserTarget(sender, bot, context['private_id']), rule, check)
        if state == 'stale' and not self.fresh(bot, version):
            return STALE
        status = {
            'sent': '该答案仅限有权成员查看，已私聊发送给你。',
            'degraded': '该答案仅限有权成员查看，已私聊发送给你（部分格式改为文字）。',
            'failed': f'该答案仅限有权成员查看，私聊发送失败（平台代码 {error or "未知"}）。请先私聊 Bot 一次后重试，或联系主人。',
            'unknown': '该答案仅限有权成员查看，私聊发送结果未知，请先查看私聊，勿重复索取。',
            'stale': '资格已变化，请重新提问。',
            'partial': '该答案仅限有权成员查看，已私聊发送部分内容；资格已变化，其余内容未发送。',
        }[state]
        # Never fall back to sending the restricted answer in the group.
        await self.reply(event, status)
        return None

    async def linked(self, event, rule_id):
        """A rule button: reload the rule now and check scope and the clicker's own eligibility."""
        group = event.group_id if event.is_group else ''
        member = event.raw_user_id or event.user_id

        async def answer(version):
            rule = await self.qa.manage(event.appid, NAMESPACE, 'get', key=rule_id) if rule_id else None
            decision = None
            enabled = bool(rule) and await self.enabled(event)
            # Same private group context as a typed question.
            contexts, context = await self.private_scope(event) if enabled and not group else ({}, None)
            in_scope = enabled and (not rule.get('groups') or group in rule['groups'] or context and context['group'] in rule['groups'])
            if in_scope and rule['enabled'] and rule['kind'] == 'text':
                decision = await self.access.admit(event.appid, rule, group, member, context=context, groups=list(contexts))
            if decision is None or decision.get('choose'):
                if not self.fresh(event.appid, version):
                    return STALE
                await self.reply(event, '该答案已失效，请重新提问。')
                return None
            return await self.deliver(event, {'rule_id': rule['id']}, {rule['id']: (rule, decision)}, version)

        await self.resolved(event, answer)

    async def choose_group(self, event, argument):
        """Private chat: pick, switch or clear the group context among the asker's valid ones."""
        bot, member = event.appid, event.raw_user_id or event.user_id
        if not await self.enabled(event):
            return event
        contexts, _ = await self.private_scope(event)
        options = '可选：' + '、'.join(contexts) if contexts else '当前没有可用的群。'
        if not argument:
            self.private_groups.pop((bot, member), None)
            await self.reply(event, '已清除群选择。' + options)
        elif argument in contexts:
            self.private_groups[bot, member] = argument
            await self.reply(event, f'已选择群 {argument}，请重新提问。')
        else:
            # Says nothing about that group's rules; the asker's own valid groups only.
            await self.reply(event, '无法选择该群：须为已启用问答、你已完成身份绑定且仍在群内的群。' + options)
        return None

    async def on_event(self, event):
        text = (event.content or '').strip()
        if event.event_type not in MESSAGE_TYPES or event.is_channel or not text:
            return event
        if text.startswith(COMMAND) and owner(event):
            return await self.command(event, text)
        if text.startswith(COMMAND) and event.is_group:
            return await self.delegated_command(event, text)
        bot = event.appid
        if event.event_type == 'GROUP_MESSAGE_CREATE' and cfg.get_bot_setting(bot, 'non_at_message.strip_bot_name_at', False):
            text = _strip_leading_bot_name_at(text, getattr(self.sender(bot), '_bot_name', '') or '').strip()
        try:
            if text == SELECT or text.startswith(SELECT + ' '):
                return await self.select_text(event, text[len(SELECT) :].strip())
            if not event.is_group and (text == CHOOSE_GROUP or text.startswith(CHOOSE_GROUP + ' ')):
                return await self.choose_group(event, text[len(CHOOSE_GROUP) :].strip())

            async def answer(version):
                candidates, admitted, choices = await self.matched(event, text)
                if not candidates:
                    if choices:
                        await self.reply(event, f'该问题需要先选择群，请发送：{CHOOSE_GROUP} <群ID>。可选：' + '、'.join(choices))
                        return None
                    return event
                if not self.fresh(bot, version):
                    return STALE
                if len(candidates) == 1:
                    return await self.deliver(event, candidates[0], admitted, version)
                return await self.offer(event, self.menus.issue(context_of(event), NAMESPACE, text, candidates), candidates, 0, admitted, version)

            return await self.resolved(event, answer)
        except (sqlite3.Error, OSError, RuntimeError):
            # Storage trouble never blocks plugin handlers.
            return event

    async def offer(self, event, token, candidates, page, admitted, version):
        page, last, shown = self.menus.page(candidates, page)
        start = page * PAGE
        names, reports = await self.titles(event, shown, admitted)

        def current():
            """Final non-awaiting gate before each menu send: every listed rule's decision and shown title's report."""
            bot = event.appid
            return all(self.access.current(bot, admitted[c['rule_id']][1]) for c in shown) and all(
                self.access.report_current(bot, rule, event.group_id, report) for rule, report in reports
            )

        if not self.fresh(event.appid, version):
            return STALE
        if not current():
            await self.reply(event, '资格已变化，请重新提问。')
            return None
        lines = ['找到多个答案，请选择：'] + [f'{start + i + 1}. {name}' for i, name in enumerate(names)]
        if last:
            lines.append(f'第 {page + 1}/{last + 1} 页，发送：{SELECT} 页 <页码>')
        lines.append(f'也可发送：{SELECT} <序号>')
        text = '\n'.join(lines)
        caller = event.raw_user_id or event.user_id
        rows = [
            [{'text': name[:20], 'type': 1, 'data': f'{CALLBACK}{token}:{start + i}', 'list': [caller], 'tips': f'请发送：{SELECT} {start + i + 1}'}]
            for i, name in enumerate(names)
        ]
        navigation = [
            {'text': label, 'type': 1, 'data': f'{CALLBACK}{token}:p{target}', 'list': [caller], 'tips': f'请发送：{SELECT} 页 {target + 1}'}
            for label, target in (('上一页', page - 1), ('下一页', page + 1))
            if 0 <= target <= last
        ]
        if navigation:
            rows.append(navigation)
        if cfg.get_bot_setting(event.appid, 'message.use_markdown', True):
            try:
                result = outcome(await self.reply(event, text, buttons=rows))
            except Exception:  # noqa: BLE001 - a transport error leaves the menu's delivery unknown
                result = 'unknown'
            # Only a definite rejection falls back; an unknown result is not re-sent.
            if result != 'rejected':
                return None
        # The rejected menu delivered nothing: re-resolve or report, never offer stale titles.
        if not self.fresh(event.appid, version):
            return STALE
        if not current():
            await self.reply(event, '资格已变化，请重新提问。')
            return None
        await self.reply(event, text + '\n按钮暂不可用，请使用以上文字选择。')
        return None

    async def choose(self, event, menu, token, action):
        """Re-read current rules for this caller/context; stale choices answer nothing."""
        if menu.get('kind') == 'links':
            index = int(action[1:]) if action[:1] == 'r' and action[1:].isdecimal() else -1
            await self.linked(event, menu['links'][index] if 0 <= index < len(menu['links']) else None)
            return

        async def answer(version):
            candidates, admitted = await self.current(event, menu)
            if not self.fresh(event.appid, version):
                return STALE
            if action.startswith('p'):
                menu['rules'] = [c['rule_id'] for c in candidates]
                if not candidates:
                    await self.reply(event, '这些答案已变化，请重新提问。')
                    return None
                return await self.offer(event, token, candidates, int(action[1:]), admitted, version)
            index = int(action)
            rule_id = menu['rules'][index] if 0 <= index < len(menu['rules']) else None
            chosen = next((c for c in candidates if c['rule_id'] == rule_id), None)
            if chosen is None:
                await self.reply(event, '该答案已失效，请重新提问。')
                return None
            return await self.deliver(event, chosen, admitted, version)

        await self.resolved(event, answer)

    async def select_text(self, event, argument):
        menu, reason = self.menus.resolve(event)
        if menu is None:
            # No menu of this caller here: leave the text to plugins and defaults.
            return event if reason == 'expired' else None
        words = argument.split()
        if len(words) == 2 and words[0] == '页' and words[1].isdecimal():
            action = f'p{int(words[1]) - 1}'
        elif len(words) == 1 and words[0].isdecimal() and int(words[0]) >= 1:
            action = ('r' if menu.get('kind') == 'links' else '') + str(int(words[0]) - 1)
        else:
            await self.reply(event, f'请发送：{SELECT} <序号> 或 {SELECT} 页 <页码>')
            return None
        await self.choose(event, menu, menu['token'], action)
        return None

    async def on_interaction(self, event):
        """Button callbacks (before_event): ACK at once, answer separately, keep the event for framework logging."""
        payload = event.content or ''
        if event.event_type != 'INTERACTION_CREATE' or not payload.startswith(CALLBACK):
            return event
        event.set_callback_code(0)
        appid = event.appid
        if (
            event.is_channel
            or not (event.is_group or event.is_direct)
            or str(event.user_id) in get_blacklist_map(appid, 'user')
            or event.group_id
            and str(event.group_id) in get_blacklist_map(appid, 'group')
            or cfg.get_bot_setting(appid, 'maintenance.enabled', False)
            and not owner(event)
        ):
            return event
        parts = payload[len(CALLBACK) :].split(':')
        if len(parts) != 2 or not (parts[1].isdecimal() or parts[1][:1] in {'p', 'r'} and parts[1][1:].isdecimal()):
            return event
        try:
            menu, reason = self.menus.resolve(event, parts[0])
            if menu is None:
                await self.reply(event, '这不是你的选择菜单。' if reason == 'context_mismatch' else '菜单已失效，请重新提问。')
            else:
                await self.choose(event, menu, parts[0], parts[1])
        except (sqlite3.Error, OSError, RuntimeError):
            await self.reply(event, '问答暂不可用，请稍后重试。')
        return event

    async def delegated_command(self, event, text):
        """Current group owner/admin, only where the owner opened delegation, only this group's rules."""
        bot, group = event.appid, event.group_id
        decision = await self.access.delegate(bot, group, event.raw_user_id or event.user_id)
        if decision is None:
            return event
        try:
            data = json.loads(text[len(COMMAND) :])
            if not isinstance(data, dict):
                raise ValueError('操作须为对象')
            values = dict(data)
            operation = values.pop('operation', '')
            if operation not in {'list', 'create', 'update', 'delete'}:
                raise ValueError('委托只能管理本群规则：list/create/update/delete')
            if set(values) - {'id', 'name', 'target', 'keywords', 'mode', 'priority', 'enabled'}:
                raise ValueError('委托不能修改适用群、访问方式、授权策略或委托设置')
            own = lambda rule: rule.get('groups') == [group]  # noqa: E731

            def guard():
                return self.access.current(bot, decision)

            if operation == 'list':
                result = await self.delegated_list(event, own, decision)
            elif operation == 'create':
                if 'id' in values:
                    raise ValueError('新增不接受 id')
                rule = await self.qa.manage(bot, NAMESPACE, 'create', {'kind': 'text', **values, 'groups': [group]}, guard=guard)
                result = {'id': rule['id'], 'name': rule['name']}
            else:
                key = values.pop('id', '')
                rule = await self.qa.manage(bot, NAMESPACE, operation, values if operation == 'update' else None, key, guard=guard, scope=own)
                result = {'id': rule['id']}
            content = json.dumps(result, ensure_ascii=False)
        except KeyError:
            content = '本群没有该规则'
        except PermissionError as error:
            content = str(error)
        except (ValueError, TypeError) as error:
            content = str(error)
        except (sqlite3.Error, OSError, RuntimeError):
            content = '问答存储暂不可用'
        await self.reply(event, content)
        return None

    async def delegated_list(self, event, own, decision):
        """This group's rules for the group, read and authorized again while they change before anything is sent.

        Restricted details go to the delegate's verified private identity; the
        group only learns their count and what happened to that push. Reads
        have no store lock to guard them, so every reply re-checks after its waits.
        """
        bot, group, member = event.appid, event.group_id, event.raw_user_id or event.user_id
        for attempt in range(ATTEMPTS):
            if attempt:
                # Rule changes also renew the authorization inventory: decide again before re-reading.
                decision = await self.access.delegate(bot, group, member)
            if not (self.qa.active and decision):
                raise PermissionError(MANAGER_CHANGED)
            version = self.qa.version(bot, NAMESPACE)
            rules = [r for r in await self.qa.manage(bot, NAMESPACE, 'list') if own(r)]
            # Managing is not disclosure: the group sees public rules and only a count of restricted ones.
            fields = ('id', 'name', 'keywords', 'mode', 'enabled')
            public = [{k: r[k] for k in fields} for r in rules if r.get('access') != 'restricted']
            hidden = [{k: r[k] for k in fields} for r in rules if r.get('access') == 'restricted']
            result = {'rules': public, 'restricted': len(hidden)}
            status = await self.manager_details(event, hidden, decision, version) if hidden else None
            if status is STALE:
                continue
            # Final non-awaiting gate: the manager still qualifies and no listed title or keyword changed.
            manager = self.qa.active and self.access.current(bot, decision)
            if manager and self.fresh(bot, version):
                return {**result, 'restricted_detail': status} if status else result
            if status:
                # The push cannot be undone; the group gets no list, only what happened to the push.
                raise PermissionError(('规则已变化，请重新发送' if manager else MANAGER_CHANGED) + f'（受限详情：{status}）')
        if not (self.qa.active and await self.access.delegate(bot, group, member)):
            raise PermissionError(MANAGER_CHANGED)
        raise PermissionError('规则正在变化，请稍后重新发送')

    async def manager_details(self, event, details, decision, version):
        """Restricted rule details to the delegate's verified private identity.

        Returns the status for the group, or STALE when the manager decision or
        the rules changed before anything was pushed.
        """
        bot, group, member = event.appid, event.group_id, event.raw_user_id or event.user_id
        found = await self.access.manager_context(bot, group, member)
        context, sender = found['context'], self.sender(bot)
        if not (self.qa.active and self.access.current(bot, decision) and self.fresh(bot, version)):
            return STALE
        if context is None or sender is None:
            if found['reason'] == 'binding_missing':
                return f'受限规则详情只私聊发送，请先私聊 Bot 发送：身份绑定 申请 {group} {member}'
            return '受限规则详情只私聊发送，暂不能私聊发送'
        if not self.access.context_current(bot, context):
            raise PermissionError(MANAGER_CHANGED)
        state, error = await UserTarget(sender, bot, context['private_id']).text(json.dumps(details, ensure_ascii=False), msg_type=TEXT)
        return {
            'sent': '已私聊发送',
            'rejected': f'私聊发送失败（平台代码 {(error or {}).get("code", "未知")}）',
            'unknown': '私聊发送结果未知，请先查看私聊',
        }[state]

    async def command(self, event, text):
        sender = self.sender(event.appid)
        if sender is None:
            return None
        try:
            result = await self.execute(event.appid, json.loads(text[len(COMMAND) :]))
            content = '已处理，请在主人私聊或后台查看详情' if event.is_group else json.dumps(result, ensure_ascii=False)
        except (ValueError, TypeError, KeyError) as error:
            content = str(error)
        except (sqlite3.Error, OSError, RuntimeError):
            content = '问答存储暂不可用'
        await sender.reply(event, content)
        return None

    async def http(self, request):
        from web.auth import authorize_request

        denied = authorize_request(request)
        if denied is not None:
            return denied
        if request.path.rstrip('/') != PREFIX.rstrip('/'):
            return web.json_response({'success': False, 'error': '入口不存在'}, status=404)
        bot = request.query.get('bot', '')
        if not cfg.get_bot_config(bot):
            return web.json_response({'success': False, 'error': '请选择有效 Bot'}, status=404)
        try:
            if request.method == 'GET':
                result = await self.execute(bot, {'operation': 'list'})
            elif request.method == 'POST':
                result = await self.execute(bot, await request.json())
            else:
                return web.json_response({'success': False, 'error': '方法不存在'}, status=405)
            return web.json_response({'success': True, 'data': result}, headers={'Cache-Control': 'no-store'})
        except (ValueError, TypeError, KeyError) as error:
            return web.json_response({'success': False, 'error': str(error)}, status=422)
        except (sqlite3.Error, OSError, RuntimeError):
            return web.json_response({'success': False, 'error': '问答存储暂不可用'}, status=503)
