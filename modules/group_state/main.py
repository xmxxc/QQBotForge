"""Independent group facts module with owner HTTP and QQ management."""

from pathlib import Path

from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .state import PREFIX, GroupState

__module_meta__ = {'name': '群状态', 'description': '按 Bot 查看和刷新已知群及平台状态', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = GroupState(ctx.get_data_path('group-state.db'))
    try:
        await candidate.initialize()
        candidate.connect(ctx)
        ctx.register_hook('before_event', candidate.on_event, priority=0)
        register_route('*', PREFIX + '*', candidate.http, auth=True)
        register_page('group-state', '群状态', source='module', source_name='group_state', html_file=str(Path(__file__).with_name('page.html')))
        await ctx.emit('group_state_available', candidate)
    except BaseException:
        unregister_route('*', PREFIX + '*')
        unregister_page('group-state')
        candidate.close()
        raise
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    unregister_route('*', PREFIX + '*')
    unregister_page('group-state')
    if _instance is not None:
        _instance.close()
        _instance = None
