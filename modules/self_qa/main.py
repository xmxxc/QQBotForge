"""Reusable self-service question/answer module; no automatic message subscription."""

from .matching import QuestionAnswer

__module_meta__ = {'name': '自助问答', 'description': '按 Bot 与消费方匹配精确/包含关键词及静态回复', 'version': '1.0.0'}
_instance = None


async def setup(ctx):
    global _instance
    candidate = QuestionAnswer(ctx.get_data_path('self-qa.db'))
    await candidate.initialize()
    _instance = candidate
    return candidate


async def teardown():
    global _instance
    if _instance is not None:
        _instance.close()
        _instance = None
