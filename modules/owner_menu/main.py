"""Personal owner operations; no framework or production startup changes."""
from .service import OwnerMenu

__module_meta__ = {'name': '主人菜单', 'description': '按钮管理群状态、授权、忍3与更新', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = OwnerMenu(ctx)
    ctx.register_hook('before_event', candidate.on_event, priority=20)
    ctx.register_hook('group_state_available', candidate.available)
    candidate.subscription = ctx.subscribe_connection_status(candidate.connection)
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    if _instance:
        await _instance.close()
    _instance = None
