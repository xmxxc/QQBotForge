"""Rich answers (text, Markdown, image, buttons) with confirmed-only fallback.

``Sender.reply`` returns the platform response dict: an error carries ``code``
(``-1`` is the framework's network failure; an HTTP 5xx without a JSON body
carries the status), otherwise the message was sent. Only a definite rejection
falls back to plain text; an unknown result never re-sends the answer. Every
part is sent only after ``check()`` confirms the answer is still current.
Buttons carry session references, never answer content.
"""

import time

from core.base.config import cfg
from core.message.media import upload_media_via_url

TEXT, MARKDOWN = 0, 2
IMAGE = 1
ACTIONS = {'command', 'link', 'rule'}


def outcome(result, *, media=False):
    """'sent', 'rejected' or 'unknown' for one reply result."""
    if result is None:
        # Media helpers return None when the upload failed before any send.
        return 'rejected' if media else 'unknown'
    if not isinstance(result, dict):
        return 'unknown'
    code = result.get('code')
    if code in (None, 0):
        return 'sent'
    # Network failure or a server error: the platform may still have delivered it.
    return 'unknown' if str(code) == '-1' or isinstance(code, int) and 500 <= code <= 599 else 'rejected'


def validate_rich(rule):
    """Format, image and buttons of a text answer; raises ValueError for the owner."""
    rule.setdefault('format', 'text')
    if rule['format'] not in {'text', 'markdown'}:
        raise ValueError('回复格式须为 text 或 markdown')
    image = rule.setdefault('image', None)
    if image is not None and (not isinstance(image, str) or not image.startswith(('https://', 'http://')) or len(image) > 2000):
        raise ValueError('图片须为 http(s) 链接，最多 2000 字符')
    buttons = rule.setdefault('buttons', [])
    if not isinstance(buttons, list) or len(buttons) > 10:
        raise ValueError('按钮最多 10 个')
    for button in buttons:
        if not isinstance(button, dict) or set(button) != {'label', 'action', 'value'} or button['action'] not in ACTIONS:
            raise ValueError('按钮须为 {label, action: command/link/rule, value}')
        label, value = button['label'], button['value']
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40 or not isinstance(value, str) or not value.strip():
            raise ValueError('按钮标题 1 至 40 字，值不能为空')
        if button['action'] == 'link' and (not value.startswith(('https://', 'http://')) or len(value) > 2000):
            raise ValueError('链接按钮须为 http(s) 链接')
        if button['action'] == 'command' and len(value) > 200:
            raise ValueError('指令按钮最多 200 字')
        if button['action'] == 'rule' and len(value) > 64:
            raise ValueError('规则按钮须填写规则 ID')


class Delivery:
    def __init__(self, entry):
        self.entry = entry
        self.rejections = {}

    def capabilities(self, bot):
        markdown = cfg.get_bot_setting(bot, 'message.use_markdown', True)
        return {
            'markdown': 'configured' if markdown else 'disabled_by_message.use_markdown',
            'buttons': 'with_markdown' if markdown else 'unavailable_without_markdown',
            'image': 'url_upload',
            'last_rejections': {kind: dict(info) for (b, kind), info in self.rejections.items() if b == bot},
        }

    def _reject(self, bot, kind, result):
        self.rejections[(bot, kind)] = {'code': str((result or {}).get('code', 'upload_failed'))[:64], 'at': time.time()}

    async def send(self, event, rule, buttons_token, check):
        """Send one rule's answer to this event's own chat.

        Returns 'sent', 'degraded', 'failed' or 'unknown'; 'stale' when ``check()``
        failed before anything was delivered, 'partial' when it failed after a part.
        """
        bot, entry = event.appid, self.entry
        caller = event.raw_user_id or event.user_id
        rows, lines = [], []
        for index, button in enumerate(rule.get('buttons', [])):
            label, value = button['label'], button['value']
            if button['action'] == 'link':
                rows.append([{'text': label, 'link': value}])
                lines.append(f'{label}：{value}')
            elif button['action'] == 'command':
                rows.append([{'text': label, 'type': 2, 'data': value}])
                lines.append(f'{label}：发送「{value}」')
            else:
                rows.append([{'text': label, 'type': 1, 'data': f'qa:{buttons_token}:r{index}', 'list': [caller]}])
                lines.append(f'{label}：发送「问答选择 {index + 1}」')
        rich = bool(rows) or rule.get('format') == 'markdown'
        state = 'sent'
        if not check():
            return 'stale'
        if rich and cfg.get_bot_setting(bot, 'message.use_markdown', True):
            result = await self._reply(event, rule['target'], buttons=rows or None, msg_type=MARKDOWN)
            state = outcome(result)
            if state == 'rejected':
                self._reject(bot, 'buttons' if rows else 'markdown', result)
        if not rich or state == 'rejected' or not cfg.get_bot_setting(bot, 'message.use_markdown', True):
            if not check():
                # A rejected first attempt delivered nothing.
                return 'stale'
            text = '\n'.join([rule['target'], *lines]) if rich else rule['target']
            result = await self._reply(event, text, msg_type=TEXT)
            state = {'sent': 'degraded' if rich else 'sent', 'rejected': 'failed', 'unknown': 'unknown'}[outcome(result)]
            if state == 'failed':
                self._reject(bot, 'text', result)
        if state not in {'sent', 'degraded'} or not rule.get('image'):
            # Unknown or failed: never send the rest blindly.
            return state
        if not check():
            return 'partial'
        try:
            # Upload, check again, then post: the upload is a wait like any other.
            sender = entry.sender(bot)
            file_info = await upload_media_via_url(sender, event, rule['image'], IMAGE) if sender else None
            if file_info and not check():
                return 'partial'
            image = await sender.reply(event, media={'file_info': file_info}) if file_info else None
        except Exception:  # noqa: BLE001 - an exception leaves the delivery unknown
            return 'unknown'
        image_state = outcome(image, media=True)
        if image_state == 'rejected':
            self._reject(bot, 'image', image if isinstance(image, dict) else None)
            if not check():
                return 'partial'
            fallback = outcome(await self._reply(event, f'图片：{rule["image"]}', msg_type=TEXT))
            return 'degraded' if fallback == 'sent' else 'unknown' if fallback == 'unknown' else 'failed'
        return state if image_state == 'sent' else 'unknown'

    async def _reply(self, event, content, **kwargs):
        try:
            return await self.entry.reply(event, content, **kwargs)
        except Exception:  # noqa: BLE001 - transport errors make the result unknown
            return {'code': -1}
