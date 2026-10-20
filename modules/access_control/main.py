"""Optional reusable authorization module, enabled through the existing module manager."""

from pathlib import Path

from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .authorization import Authorization
from .management import PREFIX, Management
from .membership import MembershipReader

__module_meta__ = {'name': '黑白名单授权', 'description': '按 Bot、群、原生成员和资源解释资格', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = Authorization(ctx.get_data_path('access-control.db'), reader=MembershipReader())
    try:
        await candidate.initialize()
        management = Management(candidate)
        ctx.register_hook('group_state_available', candidate.group_state_available, priority=0)
        ctx.register_hook('before_event', management.on_event, priority=10)
        register_route('*', PREFIX + '*', management.http, auth=True)
        register_page('access-control', '授权管理', source='module', source_name='access_control', html_file=str(Path(__file__).with_name('page.html')))
    except BaseException:
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
