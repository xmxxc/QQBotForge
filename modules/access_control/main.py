"""Optional reusable authorization module, enabled through the existing module manager."""

from pathlib import Path

from core.plugin.web_pages import match_route, register_page, register_route, unregister_page, unregister_route

from .authorization import Authorization
from .management import PREFIX, Management
from .membership import MembershipReader

__module_meta__ = {'name': '黑白名单授权', 'description': '按 Bot、群、原生成员和资源解释资格', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = Authorization(ctx.get_data_path('access-control.db'), reader=MembershipReader())
    management = Management(candidate)
    available, on_event, handler, page_registered = candidate.group_state_available, management.on_event, management.http, False
    try:
        await candidate.initialize()
        ctx.register_hook('group_state_available', available, priority=0)
        ctx.register_hook('before_event', on_event, priority=10)
        register_route('*', PREFIX + '*', handler, auth=True)
        register_page('access-control', '授权管理', source='module', source_name='access_control', html_file=str(Path(__file__).with_name('page.html')))
        page_registered = True
    except BaseException:
        # The framework only drops module hooks on disable; a failed candidate releases what it registered itself.
        from core.application import get_app

        app = get_app()
        if app is not None:
            app.hook_manager.unregister('group_state_available', available)
            app.hook_manager.unregister('before_event', on_event)
        route = match_route('*', PREFIX + '*')
        if route is not None and route['handler'] is handler:
            unregister_route('*', PREFIX + '*')
        if page_registered:
            unregister_page('access-control')
        candidate.close()
        raise
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    unregister_route('*', PREFIX + '*')
    unregister_page('access-control')
    if _instance is not None:
        _instance.close()
        _instance = None
