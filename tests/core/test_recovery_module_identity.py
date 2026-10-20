"""真实模块生命周期中，展示名不能决定旧写入者的隔离范围。"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from core.module.manager import ModuleContext, ModuleManager
from tests.core import test_resume_processing as recovery_helpers
from tests.core.test_connection_status import until
from tests.core.test_resume_processing import login

wired = recovery_helpers.wired


@pytest.fixture
def writer_state(monkeypatch):
    state = SimpleNamespace(
        entered=asyncio.Event(), cancelled=asyncio.Event(), release=asyncio.Event(),
        done=asyncio.Event(), value='initial', calls=0, received=[],
    )
    monkeypatch.setitem(sys.modules, 'recovery_identity_state', state)
    yield state
    state.release.set()


def write_consumer(directory, name, event_type, *, stubborn=True):
    directory.mkdir(parents=True, exist_ok=True)
    callback = '''    state.calls += 1
    state.entered.set()
    while not state.release.is_set():
        try:
            await state.release.wait()
        except asyncio.CancelledError:
            state.cancelled.set()
    state.value = 'late write from old generation'
    state.done.set()
'''
    (directory / 'main.py').write_text(
        'import asyncio\nimport recovery_identity_state as state\n'
        f'__module_meta__ = {{"name": {name!r}}}\n'
        'async def consume(delivery):\n'
        + (callback if stubborn else '    state.received.append(delivery.event.event_type)\n')
        + '    delivery.acknowledge()\n'
        'def setup(ctx):\n'
        f'    return ctx.subscribe_recovery_events(consume, event_types={{{event_type!r}}})\n'
    )


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
async def test_refresh_renamed_module_waits_for_writer_then_requires_fresh_rebuild(wired, tmp_path, writer_state, event_type):
    ctx, platform, client, ordinary = wired
    root = tmp_path / 'modules'
    directory = root / 'consumer'
    write_consumer(directory, 'Original consumer', event_type)
    manager = ModuleManager(str(root), ctx._hooks)
    manager.discover()
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    first = await login(ctx, platform)
    old = manager.get('consumer')
    assert old.establish(old.get_status('sample-bot'))
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'group'})
        await until(writer_state.entered.is_set)
        write_consumer(directory, 'Renamed consumer after update', event_type)
        assert await manager.refresh_and_enable('consumer')
        await until(writer_state.cancelled.is_set)
        new = manager.get('consumer')
        assert old.owner == 'Original consumer'
        assert new.owner == 'Renamed consumer after update'
        checkpoint = new.get_status('sample-bot')
        writer_state.value = 'premature rebuild'
        assert not new.establish(checkpoint)
        assert checkpoint.reason == 'old_callback_running'
        first.feed(0, t=event_type, s=3, d={'id': 'guild', 'group_openid': 'group'})
        await until(lambda: len(ordinary) == 2)
        assert writer_state.calls == 1
        checkpoint = new.get_status('sample-bot')
        writer_state.release.set()
        await until(writer_state.done.is_set)
        await until(lambda: new.get_status('sample-bot').revision > checkpoint.revision)
        assert not new.establish(checkpoint)
        assert writer_state.value == 'late write from old generation'
        fresh = new.get_status('sample-bot')
        writer_state.value = 'authoritative rebuild after writer exit'
        assert new.establish(fresh)
        assert new.get_status('sample-bot').ready
    finally:
        writer_state.release.set()
        await manager.shutdown()


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
@pytest.mark.parametrize('separate_roots', [False, True])
async def test_same_display_name_does_not_block_independent_module(wired, tmp_path, writer_state, event_type, separate_roots):
    ctx, platform, client, ordinary = wired
    root = tmp_path / 'modules'
    other_root = tmp_path / 'other-modules' if separate_roots else root
    other_key = 'consumer' if separate_roots else 'independent'
    write_consumer(root / 'consumer', 'Same display name', event_type)
    write_consumer(other_root / other_key, 'Same display name', 'GUILD_DELETE', stubborn=False)
    manager = ModuleManager(str(root), ctx._hooks)
    other = ModuleManager(str(other_root), ctx._hooks) if separate_roots else manager
    for item in {manager, other}:
        item.discover()
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    assert await other.enable(other_key, _skip_deps=True, _persist=False)
    slow, fast = manager.get('consumer'), other.get(other_key)
    first = await login(ctx, platform)
    for sub in (slow, fast):
        assert sub.establish(sub.get_status('sample-bot'))
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'group'})
        await until(writer_state.entered.is_set)
        assert await manager.disable('consumer', _persist=False)
        await until(writer_state.cancelled.is_set)
        assert slow.owner == fast.owner
        assert fast.get_status('sample-bot').ready
        first.feed(0, t='GUILD_DELETE', s=3, d={'id': 'other-guild'})
        await until(lambda: writer_state.received == ['GUILD_DELETE'])
        await until(lambda: fast.get_status('sample-bot').ready)
    finally:
        writer_state.release.set()
        await manager.shutdown()
        if other is not manager:
            await other.shutdown()


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
@pytest.mark.parametrize('replacement', ['new_manager', 'root_alias', 'entry_symlink_update'])
async def test_installation_scope_survives_manager_recreation_and_entry_update(wired, tmp_path, writer_state, event_type, replacement):
    ctx, platform, client, ordinary = wired
    root = tmp_path / 'modules'
    entry = root / 'consumer'
    root.mkdir()
    if replacement == 'entry_symlink_update':
        first_version = tmp_path / 'versions' / 'one'
        write_consumer(first_version, 'Original name', event_type)
        entry.symlink_to(first_version, target_is_directory=True)
    else:
        write_consumer(entry, 'Original name', event_type)
    manager = ModuleManager(str(root), ctx._hooks)
    manager.discover()
    assert await manager.enable('consumer', _skip_deps=True, _persist=False)
    first = await login(ctx, platform)
    old = manager.get('consumer')
    assert old.establish(old.get_status('sample-bot'))
    successor = None
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'group'})
        await until(writer_state.entered.is_set)
        assert await manager.disable('consumer', _persist=False)
        await until(writer_state.cancelled.is_set)
        if replacement == 'entry_symlink_update':
            second_version = tmp_path / 'versions' / 'two'
            write_consumer(second_version, 'New version display name', event_type)
            entry.unlink()
            entry.symlink_to(second_version, target_is_directory=True)
        else:
            write_consumer(entry, 'New version display name', event_type)
        if replacement == 'root_alias':
            alias = tmp_path / 'module-root-alias'
            alias.symlink_to(root, target_is_directory=True)
            successor = ModuleManager(str(alias), ctx._hooks)
        else:
            successor = ModuleManager(str(root), ctx._hooks)
        successor.discover()
        assert await successor.enable('consumer', _skip_deps=True, _persist=False)
        new = successor.get('consumer')
        assert new.owner == 'New version display name'
        checkpoint = new.get_status('sample-bot')
        assert not new.establish(checkpoint)
        assert checkpoint.reason == 'old_callback_running'
        writer_state.release.set()
        await until(writer_state.done.is_set)
        await until(lambda: new.get_status('sample-bot').revision > checkpoint.revision)
        assert not new.establish(checkpoint)
        assert new.establish(new.get_status('sample-bot'))
    finally:
        writer_state.release.set()
        await manager.shutdown()
        if successor is not None:
            await successor.shutdown()


@pytest.mark.parametrize('event_type', ['GUILD_UPDATE', 'GROUP_MEMBER_ADD'])
@pytest.mark.parametrize('location', [
    'same_directory', 'root_alias', 'parent_symlink_dotdot', 'parent_symlink_dotdot_relative',
    'parent_symlink_dotdot_slash', 'parent_symlink_dotdot_dot', 'parent_symlink_dotdot_independent',
    'terminal_dotdot', 'current_directory', 'different_directory',
])
async def test_direct_context_scope_ignores_display_mutation_and_normalizes_location(wired, tmp_path, writer_state, monkeypatch, event_type, location):
    ctx, platform, client, ordinary = wired
    root = tmp_path / 'direct-modules'
    directory = root / 'consumer'
    directory.mkdir(parents=True)
    if location.startswith('parent_symlink_dotdot'):
        nested = root / 'nested'
        nested.mkdir()
        lexical = tmp_path / 'lexical'
        lexical.mkdir()
        (lexical / 'alias').symlink_to(nested, target_is_directory=True)
        original_directory = lexical / 'alias' / '..' / 'consumer'
        if location == 'parent_symlink_dotdot_relative':
            monkeypatch.chdir(tmp_path)
            original_directory = original_directory.relative_to(tmp_path)
        elif location == 'parent_symlink_dotdot_slash':
            original_directory = str(original_directory) + '/'
        elif location == 'parent_symlink_dotdot_dot':
            original_directory = str(original_directory) + '/.'
    elif location == 'terminal_dotdot':
        nested = directory / 'nested'
        nested.mkdir()
        alias = tmp_path / 'nested-alias'
        alias.symlink_to(nested, target_is_directory=True)
        original_directory = alias / '..'
    elif location == 'current_directory':
        monkeypatch.chdir(directory)
        original_directory = '.'
    else:
        original_directory = directory
    original_ctx = ModuleContext('Shared display name', str(original_directory), ctx._hooks)
    assert os.path.samefile(original_ctx.module_dir, directory)

    async def stubborn(delivery):
        writer_state.entered.set()
        while not writer_state.release.is_set():
            try:
                await writer_state.release.wait()
            except asyncio.CancelledError:
                writer_state.cancelled.set()
        writer_state.done.set()
        delivery.acknowledge()

    async def healthy(delivery):
        writer_state.received.append(delivery.event.event_type)
        delivery.acknowledge()

    old = original_ctx.subscribe_recovery_events(stubborn, event_types={event_type})
    first = await login(ctx, platform)
    assert old.establish(old.get_status('sample-bot'))
    new = None
    try:
        first.feed(0, t=event_type, s=2, d={'id': 'guild', 'group_openid': 'group'})
        await until(writer_state.entered.is_set)
        # 这些字段仍可用于展示；变更它们不能移动已捕获的安装槽位。
        old.owner = 'Changed subscription display name'
        original_ctx.name = 'Changed context display name'
        old.cancel()
        await until(writer_state.cancelled.is_set)
        if location == 'root_alias':
            alias = tmp_path / 'direct-root-alias'
            alias.symlink_to(root, target_is_directory=True)
            replacement_dir = alias / 'consumer'
        elif location == 'different_directory':
            replacement_dir = root / 'independent'
        elif location == 'parent_symlink_dotdot_independent':
            replacement_dir = lexical / 'consumer'
        else:
            replacement_dir = directory / '..' / 'consumer'
        new_ctx = ModuleContext('Shared display name', str(replacement_dir), ctx._hooks)
        independent = location in {'different_directory', 'parent_symlink_dotdot_independent'}
        assert os.path.samefile(original_ctx.module_dir, new_ctx.module_dir) is not independent
        new = new_ctx.subscribe_recovery_events(healthy, event_types={event_type})
        checkpoint = new.get_status('sample-bot')
        if independent:
            assert new.establish(checkpoint)
            first.feed(0, t=event_type, s=3, d={'id': 'other', 'group_openid': 'other'})
            await until(lambda: writer_state.received == [event_type])
            await until(lambda: new.get_status('sample-bot').ready)
        else:
            assert not new.establish(checkpoint)
            assert checkpoint.reason == 'old_callback_running'
            writer_state.release.set()
            await until(writer_state.done.is_set)
            await until(lambda: new.get_status('sample-bot').revision > checkpoint.revision)
            assert not new.establish(checkpoint)
            assert new.establish(new.get_status('sample-bot'))
    finally:
        writer_state.release.set()
        old.cancel()
        if new is not None:
            new.cancel()
