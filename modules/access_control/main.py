"""Optional reusable authorization module, enabled through the existing module manager."""

from .membership import MembershipReader
from .policy import AccessControl

__module_meta__ = {'name': '黑白名单授权', 'description': '按 Bot、群、原生成员和资源解释资格', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = AccessControl(ctx.get_data_path('access-control.db'), reader=MembershipReader())
    await candidate.initialize()
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None
