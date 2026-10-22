"""Rich answers (text, Markdown, image, buttons) with confirmed-only fallback.

``Sender.reply`` returns the platform response dict: an error carries ``code``
(``-1`` is the framework's network failure), otherwise the message was sent.
Pushes (``send_to_user``/``send_image``) return ``(ok, data, ...)``. An HTTP
5xx without a JSON body carries the status as ``code``. Only a definite
rejection falls back to plain text; an unknown result never re-sends the
answer. Every part is sent only after ``check()`` confirms the answer is still
current. Buttons carry session references, never answer content.
"""

import time

from core.base.config import cfg

TEXT, MARKDOWN = 0, 2
ACTIONS = {'command', 'link', 'rule'}


def unknown_code(code):
    """Network failure or a server error: the platform may still have delivered it."""
    return str(code) == '-1' or isinstance(code, int) and 500 <= code <= 599


def outcome(result, *, media=False):
    """'sent', 'rejected' or 'unknown' for one reply or push result."""
    if isinstance(result, tuple) and result and isinstance(result[0], bool):
        if result[0]:
            return 'sent'
        data = result[1] if len(result) > 1 else None
        return 'unknown' if isinstance(data, dict) and unknown_code(data.get('code')) else 'rejected'
    if result is None:
        # Media helpers return None when the upload failed before any send.
        return 'rejected' if media else 'unknown'
    if not isinstance(result, dict):
        return 'unknown'
    code = result.get('code')
    if code in (None, 0):
        return 'sent'
    return 'unknown' if unknown_code(code) else 'rejected'


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


class EventTarget:
    """Reply into the chat of the triggering event."""

    def __init__(self, entry, event, context):
        self.entry, self.event, self.context = entry, event, context

    async def text(self, content, **kwargs):
        try:
            result = await self.entry.reply(self.event, content, **kwargs)
        except Exception:  # noqa: BLE001 - transport errors make the result unknown
            return 'unknown', None
        return outcome(result), result if isinstance(result, dict) else None

    async def image(self, url):
        sender = self.entry.sender(self.event.appid)
        try:
            return outcome(await sender.reply_image(self.event, url) if sender else None, media=True)
        except Exception:  # noqa: BLE001
            return 'unknown'


class UserTarget:
    """Push to a verified private identity (``send_to_user``)."""

    def __init__(self, sender, bot, private_id):
        self.sender, self.private_id = sender, private_id
        self.context = {'bot': bot, 'caller': private_id, 'scene': 'private', 'group': ''}

    async def text(self, content, **kwargs):
        try:
            result = await self.sender.send_to_user(self.private_id, content, **kwargs)
        except Exception:  # noqa: BLE001
            return 'unknown', None
        data = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        return outcome(result), data

    async def image(self, url):
        try:
            return outcome(await self.sender.send_image('user', self.private_id, url), media=True)
        except Exception:  # noqa: BLE001
            return 'unknown'


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

    def _reject(self, bot, kind, data):
        code = str((data or {}).get('code', 'upload_failed') if isinstance(data, dict) else 'rejected')[:64]
        self.rejections[(bot, kind)] = {'code': code, 'at': time.time()}
        return code

    async def send(self, target, rule, buttons_token, check):
        """Send one rule's answer to ``target``; returns (state, platform error code or None).

        state is 'sent', 'degraded', 'failed' or 'unknown'; 'stale' when ``check()``
        failed before anything was delivered, 'partial' when it failed after a part.
        """
        bot = target.context['bot']
        caller = target.context['caller']
        error = None
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
        markdown = cfg.get_bot_setting(bot, 'message.use_markdown', True)
        if not check():
            return 'stale', None
        if rich and markdown:
            state, data = await target.text(rule['target'], buttons=rows or None, msg_type=MARKDOWN)
            if state == 'rejected':
                error = self._reject(bot, 'buttons' if rows else 'markdown', data)
        if not rich or state == 'rejected' or not markdown:
            if not check():
                # A rejected first attempt delivered nothing.
                return 'stale', error
            text = '\n'.join([rule['target'], *lines]) if rich else rule['target']
            result, data = await target.text(text, msg_type=TEXT)
            state = {'sent': 'degraded' if rich else 'sent', 'rejected': 'failed', 'unknown': 'unknown'}[result]
            if state == 'failed':
                error = self._reject(bot, 'text', data)
        if state not in {'sent', 'degraded'} or not rule.get('image'):
            # Unknown or failed: never send the rest blindly.
            return state, error
        if not check():
            return 'partial', error
        image_state = await target.image(rule['image'])
        if image_state == 'rejected':
            self._reject(bot, 'image', None)
            if not check():
                return 'partial', error
            fallback, _ = await target.text(f'图片：{rule["image"]}', msg_type=TEXT)
            return ('degraded' if fallback == 'sent' else 'unknown' if fallback == 'unknown' else 'failed'), error
        return (state if image_state == 'sent' else 'unknown'), error
