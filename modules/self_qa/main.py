"""Self-service question/answer module: public owner-configured replies plus a consumer matching API."""

from pathlib import Path

from core.plugin.web_pages import register_page, register_route, unregister_page, unregister_route

from .entry import PREFIX, PublicEntry
from .matching import QuestionAnswer

__module_meta__ = {'name': '自助问答', 'description': '主人配置公开问答并按 Bot／群／私聊范围回复；提供消费方匹配接口', 'version': '1.1.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = QuestionAnswer(ctx.get_data_path('self-qa.db'))
    try:
        await candidate.initialize()
        entry = PublicEntry(candidate)
        ctx.register_hook('before_handler', entry.on_event, priority=100)
        register_route('*', PREFIX + '*', entry.http, auth=True)
        register_page('self-qa', '自助问答', source='module', source_name='self_qa', html_file=str(Path(__file__).with_name('page.html')))
    except BaseException:
        unregister_route('*', PREFIX + '*')
        unregister_page('self-qa')
        candidate.close()
        raise
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    unregister_route('*', PREFIX + '*')
    unregister_page('self-qa')
    if _instance is not None:
        _instance.close()
        _instance = None
