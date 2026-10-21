"""Independent group facts module with owner HTTP and QQ management."""

from pathlib import Path

from core.plugin.web_pages import match_route, register_page, register_route, unregister_page, unregister_route

from .state import PREFIX, GroupState

__module_meta__ = {'name': '群状态', 'description': '按 Bot 查看和刷新已知群及平台状态', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = GroupState(ctx.get_data_path('group-state.db'))
    on_event, handler, page_registered = candidate.on_event, candidate.http, False
    try:
        await candidate.initialize()
        ctx.register_hook('before_event', on_event, priority=0)
        register_route('*', PREFIX + '*', handler, auth=True)
        register_page('group-state', '群状态', source='module', source_name='group_state', html_file=str(Path(__file__).with_name('page.html')))
        page_registered = True
    except BaseException:
        # The framework only drops module hooks on disable; a failed candidate releases what it registered itself.
        ctx._hooks.unregister('before_event', on_event)
        route = match_route('*', PREFIX + '*')
        if route is not None and route['handler'] is handler:
            unregister_route('*', PREFIX + '*')
        if page_registered:
            unregister_page('group-state')
        await candidate.aclose()
        raise
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    unregister_route('*', PREFIX + '*')
    unregister_page('group-state')
    instance, _instance = _instance, None
    if instance is not None:
        await instance.aclose()
