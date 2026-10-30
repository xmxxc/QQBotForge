"""显示名查询接口。

format_member / format_group / display_* 返回可直接拼入 QQ Markdown 或纯文字消息的文本。
名字的 ASCII 标点全角化，避免 Markdown、提及和链接被解释。身份、权限和数据关联仍用原生 ID。
"""

import asyncio
import string
import unicodedata
from time import monotonic

DISPLAY_NAME_MAX_LENGTH = 64
_GROUP_NAME_NEGATIVE_TTL = 300
_GROUP_NAME_CACHE_MAX = 1024
_MESSAGE_PUNCTUATION = str.maketrans({char: chr(ord(char) + 0xFEE0) for char in string.punctuation})
USER_NAME_UPSERT_SQL = (
    'INSERT INTO users (user_id, name) VALUES (?, ?) '
    'ON CONFLICT(user_id) DO UPDATE SET name=excluded.name '
    "WHERE excluded.name <> ''"
)


def clean_display_name(value, *, max_length=DISPLAY_NAME_MAX_LENGTH):
    """规范化存储名字；消息输出须使用 format_* / display_* 中和标点。"""
    if value is None:
        return ''
    text = str(value)
    text = ''.join(
        char for char in text
        if char not in '\r\n\u2028\u2029'
        and not unicodedata.category(char).startswith('C')
    ).strip()
    return text[:max_length]


def queue_member_name(log_service, member_id, name):
    """非阻塞记录名字；兼容尚未提供 data.db 队列的旧日志替身。"""
    native_id = str(member_id or '').strip()
    display_name = clean_display_name(name)
    db_queue = getattr(log_service, 'db_queue', None)
    if not native_id or not display_name or not callable(db_queue):
        return False
    return db_queue(USER_NAME_UPSERT_SQL, (native_id, display_name))


def _native_id(value):
    return str(value or '').strip()


def _suffix(native_id):
    return f'…{native_id[-4:]}'


def format_member(member_id, name=''):
    """返回 QQ Markdown / 纯文字安全文本；没有记录时降级为未知成员。"""
    native_id = _native_id(member_id)
    display_name = clean_display_name(name).translate(_MESSAGE_PUNCTUATION)
    label = display_name or '未知成员'
    return f'{label}（{_suffix(native_id)}）'


def format_group(group_id, name=''):
    """返回 QQ Markdown / 纯文字安全文本；没有记录时降级为未知群。"""
    native_id = _native_id(group_id)
    display_name = clean_display_name(name).translate(_MESSAGE_PUNCTUATION)
    if display_name:
        return display_name
    return f'未知群（{_suffix(native_id)}）'


async def _read_name(log_service, sql, value, column):
    if log_service is None:
        return ''
    try:
        row = await log_service.db_fetch_one(sql, (value,))
    except Exception:
        return ''
    return row.get(column, '') if isinstance(row, dict) else ''


def _get_bot(appid):
    from core.application import get_app

    app = get_app()
    if app is None:
        return None
    try:
        return app.get_bot(str(appid))
    except Exception:
        return None


async def display_member_for_bot(bot, member_id):
    """供 Application/插件宿主复用的同一查询实现。"""
    native_id = _native_id(member_id)
    name = await _read_name(
        getattr(bot, 'log_service', None),
        'SELECT name FROM users WHERE user_id=?',
        native_id,
        'name',
    )
    return format_member(native_id, name)


class _GroupNameLookup:
    """每个 Bot 独有的有界负缓存；本地新名字始终优先于此缓存。"""

    def __init__(self):
        self.misses = {}
        self.pending = {}

    async def get(self, bot, group_id):
        if self.misses.get(group_id, 0) > monotonic():
            return ''
        task = self.pending.get(group_id)
        if task is None:
            task = asyncio.create_task(self._fetch(bot, group_id))
            self.pending[group_id] = task
            task.add_done_callback(lambda done: self.pending.pop(group_id, None))
        # 一个列表/页面取消，不应取消其他显示查询共用的补查。
        return await asyncio.shield(task)

    async def _fetch(self, bot, group_id):
        name = ''
        try:
            result = await bot.sender.get_group_info(group_id, return_error=True)
            data = result[0] if isinstance(result, tuple) else result
            if isinstance(data, dict):
                name = data.get('group_name', '')
        except Exception:
            pass
        if not clean_display_name(name):
            name = await _read_name(
                getattr(bot, 'log_service', None),
                'SELECT group_name FROM groups_users WHERE group_id=?',
                group_id, 'group_name',
            )
        if clean_display_name(name):
            self.misses.pop(group_id, None)
        else:
            now = monotonic()
            if len(self.misses) >= _GROUP_NAME_CACHE_MAX:
                self.misses = {key: expiry for key, expiry in self.misses.items() if expiry > now}
                if len(self.misses) >= _GROUP_NAME_CACHE_MAX:
                    self.misses.pop(next(iter(self.misses)))
            self.misses[group_id] = now + _GROUP_NAME_NEGATIVE_TTL
        return name


async def display_group_for_bot(bot, group_id):
    """供 Application/插件宿主复用的同一查询实现。"""
    native_id = _native_id(group_id)
    log_service = getattr(bot, 'log_service', None)
    name = await _read_name(
        log_service,
        'SELECT group_name FROM groups_users WHERE group_id=?',
        native_id,
        'group_name',
    )
    if clean_display_name(name) or bot is None or not native_id:
        return format_group(native_id, name)

    lookup = getattr(bot, '_display_group_lookup', None)
    if lookup is None:
        lookup = bot._display_group_lookup = _GroupNameLookup()
    name = await lookup.get(bot, native_id)
    return format_group(native_id, name)


async def display_member(appid, member_id):
    """返回指定 Bot 下成员的显示名（异步只读查询）。"""
    return await display_member_for_bot(_get_bot(appid), member_id)


async def display_group(appid, group_id):
    """返回指定 Bot 下群的显示名；本地缺失时最多补查一次群资料。"""
    return await display_group_for_bot(_get_bot(appid), group_id)
