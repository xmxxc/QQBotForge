"""Run with PYTHONPATH set to an isolated source archive; uses fake identities only."""

import asyncio
import json
import re
import tempfile
from pathlib import Path
from types import SimpleNamespace

import core.application as application
from core.application import Application
from core.base.config import cfg
from core.base.tasks import _background_tasks
from core.message.event import Event
from core.module.manager import ModuleManager
from core.plugin.manager import PluginManager

ROOT = Path(tempfile.mkdtemp(prefix='qqbotforge-hook-proof-'))
results = []
settings = {}
cfg.get_bot_setting = lambda bot, key, default=None: settings.get((bot, key), default)
cfg.get_bot_config = lambda bot: {'owner_ids': ['owner']}
cfg.get = lambda name, key=None, default=None: default


def event(**kw):
    e = Event()
    defaults = dict(
        appid='fake-a',
        event_type='GROUP_AT_MESSAGE_CREATE',
        group_id='fake-g',
        user_id='fake-u',
        raw_user_id='fake-u',
        content='ping',
        raw_content='ping',
        raw={},
        is_group=True,
    )
    for k, v in (defaults | kw).items():
        setattr(e, k, v)
    return e


def handler(fn, **kw):
    return dict(
        func=fn,
        is_coro=True,
        compiled=re.compile('ping'),
        name='fake-handler',
        priority=1,
        event_types=None,
        group_only=False,
        direct_only=False,
        channel_only=False,
        _allowed_bots=None,
        _smask=0,
        owner_only=False,
        block=False,
        **kw,
    )


async def drain(app):
    if app._track_queue is not None:
        await app._track_queue.join()
    while _background_tasks:
        await asyncio.gather(*tuple(_background_tasks))


async def case(name, options=None, e=None, consume=True, interceptor=False, plugin=False):
    settings.clear()
    settings.update({('fake-a', 'message.send_default_response'): True})
    settings.update(options or {})
    trace = []

    class Log:
        def add_sync(self, kind, data):
            trace.append('log')

        def db_queue(self, sql, args):
            trace.append('sql')

        async def group_member_touch(self, *args, **kwargs):
            trace.append('track')
            return False

    class Sender:
        _bot_name = 'fake'

        async def reply(self, ev, *args, **kwargs):
            trace.append('reply:' + (kwargs.get('template_name') or (args[0] if args else 'custom')))

    app = Application()
    app._bot_registry = {
        'fake-a': SimpleNamespace(appid='fake-a', name='A', sender=Sender(), log_service=Log()),
        'fake-b': SimpleNamespace(appid='fake-b', name='B', sender=Sender(), log_service=Log()),
    }
    application._app = app
    manager = PluginManager(str(ROOT / 'empty-plugins'))
    app._plugin_manager = manager

    async def before(ev):
        trace.append('before_event')
        return ev

    async def hook(ev):
        trace.append('hook:' + ev.appid)
        if consume:
            await ev.reply('module')
            return None
        return ev

    async def ic(ev):
        trace.append('interceptor')
        return interceptor

    async def fn(ev, match):
        trace.append('handler')

    app.hook_manager.register('before_event', before)
    app.hook_manager.register('before_handler', hook)
    manager._all_interceptors = [dict(func=ic, is_coro=True)]
    if plugin:
        manager._all_handlers = [handler(fn)]
        manager._build_dispatch_index()
    await app._on_event(e or event())
    await drain(app)
    for t in app._track_workers:
        t.cancel()
    await asyncio.gather(*app._track_workers, return_exceptions=True)
    app._track_queue = None
    app._track_workers = []
    results.append({'case': name, 'trace': trace})
    return trace, app, manager


async def main():
    t, _, _ = await case('consume, logging, tracking, interceptor order', plugin=True)
    assert t.index('before_event') < t.index('log') < t.index('interceptor') < t.index('hook:fake-a')
    assert 'track' in t and t.count('reply:module') == 1 and 'handler' not in t and 'reply:default' not in t
    t, _, _ = await case('pass to plugin', consume=False, plugin=True)
    assert 'handler' in t and 'reply:default' not in t
    t, _, _ = await case('pass to default', consume=False)
    assert t.count('reply:default') == 1
    for kind in ['user', 'group']:
        key = 'fake-u' if kind == 'user' else 'fake-g'
        t, _, _ = await case('blacklist ' + kind, {('fake-a', f'blacklist.{kind}_list'): {key: 'blocked'}})
        assert not any(s.startswith('hook:') for s in t)
        assert 'reply:' + ('blacklist' if kind == 'user' else 'group_blacklist') in t
    t, _, _ = await case('maintenance nonowner', {('fake-a', 'maintenance.enabled'): True})
    assert 'reply:maintenance' in t and 'hook:fake-a' not in t
    t, _, _ = await case('maintenance owner', {('fake-a', 'maintenance.enabled'): True}, event(user_id='owner'))
    assert 'hook:fake-a' in t
    t, _, _ = await case('owner still blacklisted', {('fake-a', 'blacklist.user_list'): {'owner': 'blocked'}}, event(user_id='owner'))
    assert 'hook:fake-a' not in t
    for et in ['GROUP_AT_MESSAGE_CREATE', 'C2C_MESSAGE_CREATE', 'AT_MESSAGE_CREATE', 'DIRECT_MESSAGE_CREATE', 'MESSAGE_CREATE']:
        t, _, _ = await case('message type ' + et, e=event(event_type=et))
        assert 'hook:fake-a' in t
    for enabled, whitelist, expected in [(False, [], False), (True, [], True), (False, ['fake-g'], True), (False, ['other'], False)]:
        t, _, _ = await case(
            f'non-at {enabled} {whitelist}',
            {('fake-a', 'non_at_message.enabled'): enabled, ('fake-a', 'non_at_message.group_whitelist'): whitelist},
            event(event_type='GROUP_MESSAGE_CREATE'),
        )
        assert ('hook:fake-a' in t) == expected
    for flag, config in [('is_at_other_bot', 'ignore_at_other_bot'), ('is_at_other_user', 'ignore_at_other_user')]:
        for ignore in [True, False]:
            t, _, _ = await case(
                f'{flag} ignore={ignore}',
                {('fake-a', 'non_at_message.enabled'): True, ('fake-a', 'non_at_message.' + config): ignore},
                event(event_type='GROUP_MESSAGE_CREATE', **{flag: True}),
            )
            assert ('hook:fake-a' in t) == (not ignore)
        t, _, _ = await case(
            flag + ' also self at', {('fake-a', 'non_at_message.' + config): True}, event(event_type='GROUP_MESSAGE_CREATE', is_at_self=True, **{flag: True})
        )
        assert 'hook:fake-a' in t
    t, _, _ = await case('at-all dropped before dispatch', e=event(event_type='GROUP_MESSAGE_CREATE', is_at_all=True))
    assert 'hook:fake-a' not in t and 'log' in t
    t, _, _ = await case('bot sender dropped before dispatch', {('fake-a', 'non_at_message.ignore_bot_sender'): True}, event(is_bot=True))
    assert 'hook:fake-a' not in t
    t, _, _ = await case('interceptor consumes', interceptor=True)
    assert 'hook:fake-a' not in t
    t, app, pm = await case('unknown non-message', e=event(event_type='CUSTOM_TEST'), consume=False)
    assert 'hook:fake-a' not in t
    t, app, pm = await case('cross-bot B admitted independently', {('fake-a', 'maintenance.enabled'): True}, event(appid='fake-b'))
    assert 'hook:fake-b' in t and 'hook:fake-a' not in t

    # Actual application entry for return, exception, cancellation and ordering.
    settings.clear()
    settings[('fake-a', 'message.send_default_response')] = True
    app.hook_manager.clear()
    seen = []

    async def fail(ev):
        seen.append('fail')
        raise RuntimeError('synthetic')

    async def tail(ev):
        seen.append('tail')
        return ev

    app.hook_manager.register('before_handler', tail, priority=100)
    app.hook_manager.register('before_handler', fail, priority=0)
    await app._on_event(event())
    await drain(app)
    assert seen == ['fail', 'tail']
    results.append({'case': 'exception logged and priority tail continues', 'seen': seen.copy()})
    app.hook_manager.clear()
    seen.clear()

    async def cancelled(ev):
        seen.append('cancel')
        raise asyncio.CancelledError()

    app.hook_manager.register('before_handler', cancelled)
    app.hook_manager.register('before_handler', tail)
    try:
        await app._on_event(event())
        raise AssertionError('cancellation swallowed')
    except asyncio.CancelledError:
        assert seen == ['cancel']
    await drain(app)
    results.append({'case': 'cancellation propagates and no tail', 'seen': seen.copy()})

    # ModuleManager lifecycle from a synthetic module only.
    moddir = ROOT / 'modules' / 'audit_fake'
    moddir.mkdir(parents=True, exist_ok=True)
    (moddir / 'main.py').write_text(
        """async def setup(ctx):\n    @ctx.hook("before_handler")\n    async def respond(event):\n        await event.reply("lifecycle-module")\n        return None\n    return True\n"""
    )
    app.hook_manager.clear()
    mm = ModuleManager(str(moddir.parent), app.hook_manager)
    mm.discover()
    for action in ['enable', 'enable', 'reload', 'disable', 'enable']:
        if action == 'reload':
            # Existing reload installs dependencies; synthetic no-dependency module only.
            import core.module.manager as mm_module

            async def no_deps(*args, **kwargs):
                return None

            mm_module._install_deps = no_deps
        ok = await getattr(mm, action)(
            'audit_fake', **({'_skip_deps': True, '_persist': False} if action == 'enable' else {'_persist': False} if action == 'disable' else {})
        )
        assert ok
        count = len(app.hook_manager.list_hooks().get('before_handler', []))
        assert count == (0 if action == 'disable' else 1)
        await app._on_event(event())
        await drain(app)
        results.append({'case': 'module lifecycle ' + action, 'hook_count': count})
    await mm.disable('audit_fake', _persist=False)
    # Existing generic registration permits duplicates; unregister_owner removes both.
    app.hook_manager.register('before_handler', tail, owner='duplicate')
    app.hook_manager.register('before_handler', tail, owner='duplicate')
    seen.clear()
    await app._on_event(event())
    await drain(app)
    assert seen == ['tail', 'tail']
    app.hook_manager.unregister_owner('duplicate')
    assert not app.hook_manager.has('before_handler')
    results.append({'case': 'existing explicit duplicate semantics', 'seen': seen.copy()})

    app._plugin_manager = None
    app.hook_manager.register('before_handler', tail)
    seen.clear()
    await app._on_event(event())
    await drain(app)
    assert not seen
    results.append({'case': 'no plugin manager no hook'})
    for task in app._track_workers:
        task.cancel()
    await asyncio.gather(*app._track_workers, return_exceptions=True)
    (ROOT / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': len(results), 'results': str(ROOT / 'results.json')}))


asyncio.run(main())
