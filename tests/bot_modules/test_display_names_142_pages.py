"""#142 owner pages in a real browser: names shown as text, native IDs in tooltip/detail, no XSS."""

import contextlib
import sys
from pathlib import Path

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_display_names_142 import configure, stored_names
from test_group_roster import roster_platform
from test_ninja3 import ninja as ninja  # noqa: F401
from test_ninja3_page import bots_route, chrome

ROOT = Path(__file__).absolute().parents[2]
# Raw hostile text as a careless store read would return it; the pages must still treat it as text.
RAW = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>"\'&amp;'
NO_XSS = '() => window.__xss === undefined && !document.querySelector("img") && document.scripts.length === 1'


def raw_names(monkeypatch, module_name):
    """Bypass the message-safe formatting so only the page's own escaping stands between RAW and the DOM."""
    module = sys.modules[module_name]
    real = module.display_names

    async def hostile(bot, *values):
        names = await real(bot, *values)
        return {kind: {native: RAW + native.upper() for native in shown} for kind, shown in names.items()}

    monkeypatch.setattr(module, 'display_names', hostile)
    return lambda native: RAW + native.upper()


@contextlib.asynccontextmanager
async def browser_page(html_path):
    executable = chrome()
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from playwright.async_api import async_playwright

    from web import auth
    from web.api import handle_ext_route

    app = web.Application()

    async def index(request):
        return web.Response(text=Path(html_path).read_text(encoding='utf-8'), content_type='text/html')

    app.router.add_get('/', index)
    app.router.add_get('/api/bots', bots_route([{'appid': 'bot-a', 'name': 'Bot A'}]))
    app.router.add_route('*', '/api/ext/{tail:.*}', handle_ext_route)
    server = TestServer(app, host='127.0.0.1')
    await server.start_server()
    base = str(server.make_url('/'))
    errors = []
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=executable, headless=True)
            context = await browser.new_context()
            await context.add_cookies([{'name': auth.SESSION_COOKIE, 'value': 'group-test', 'url': base}])
            page = await context.new_page()
            page.set_default_timeout(5000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.errors = errors
            await page.goto(base)
            yield page
            await browser.close()
    finally:
        await server.close()


async def label(page, selector):
    """Visible text, tooltip and copyable detail of one name label."""
    return await page.eval_on_selector(
        selector, 'd => [d.querySelector("summary").textContent, d.querySelector("summary").title, d.querySelector("code").textContent]'
    )


@pytest.mark.parametrize('mode', ['stored', 'raw'])
async def test_ninja3_page_shows_names_and_escapes_them(ninja, monkeypatch, mode):
    host = ninja
    stored_names(host, users={'MEMBERA': RAW}, groups={'group-a': RAW})
    expected = raw_names(monkeypatch, 'plugins.ninja3.service') if mode == 'raw' else None
    await host.setup()
    await host.event('忍3', user='owner-a')
    await host.say('账号列表', user='MEMBERA')
    async with browser_page(ROOT / 'plugins/ninja3/page.html') as page:
        await page.wait_for_function("() => document.getElementById('status').textContent.startsWith('已加载')")
        group = await label(page, '#groups details')
        member = await label(page, '#notices details:nth-of-type(2)')
        assert await label(page, '#rows details') == group
        assert await page.evaluate(NO_XSS) and page.errors == []
    if mode == 'raw':
        assert group == [expected('group-a'), 'group-a', 'group-a'] and member == [expected('MEMBERA'), 'MEMBERA', 'MEMBERA']
    else:
        # The real path shows the message-safe display text; native IDs stay in tooltip and detail.
        assert group[1:] == ['group-a', 'group-a'] and member[1:] == ['MEMBERA', 'MEMBERA']
        assert group[0].startswith('＜img src＝x onerror＝＂window．＿＿xss＝1＂＞') and member[0].endswith('（…BERA）')


async def test_ninja3_page_degrades_unknown_names(ninja):
    host = ninja
    await host.setup()
    async with browser_page(ROOT / 'plugins/ninja3/page.html') as page:
        await page.wait_for_function("() => document.getElementById('status').textContent.startsWith('已加载')")
        assert await label(page, '#groups details') == ['未知群（…up-a）', 'group-a', 'group-a']


@pytest.mark.parametrize('mode', ['stored', 'raw'])
async def test_group_state_page_shows_names_and_escapes_them(group_host, monkeypatch, mode):
    host = group_host
    stored_names(host, users={'member-a': RAW}, groups={'group-a': RAW})
    shown = '＜img src＝x onerror＝＂window．＿＿xss＝1＂＞'
    if mode == 'raw':
        shown = raw_names(monkeypatch, type(host.state).__module__)('group-a')
    roster_platform(host)
    await host.event('普通聊天', user='member-a', group='group-a')
    async with browser_page(ROOT / 'modules/group_state/page.html') as page:
        await page.wait_for_function("() => document.getElementById('gs-status').textContent.startsWith('已更新')")
        summary = await page.text_content('#gs-summary')
        assert await page.evaluate(NO_XSS) and page.errors == []
        # Raw native IDs stay in the copyable detail.
        assert '"group": "group-a"' in await page.text_content('#gs-output')
    assert shown in summary and 'group-a' not in summary


@pytest.mark.parametrize('mode', ['stored', 'raw', 'unknown'])
async def test_access_control_page_shows_names_and_escapes_them(access_host, monkeypatch, mode):
    host = access_host
    if mode != 'unknown':
        stored_names(host, users={'member-a': RAW}, groups={'group-a': RAW})
    expected = {
        'stored': ('＜img src＝x onerror＝＂window．＿＿xss＝1＂＞', '（…er-a）'),
        'unknown': ('未知群（…up-a）', '未知成员（…er-a）'),
    }.get(mode)
    if mode == 'raw':
        raw = raw_names(monkeypatch, type(host.access).__module__.rsplit('.', 1)[0] + '.management')
        expected = (raw('group-a'), raw('member-a'))
    await configure(host)
    async with browser_page(ROOT / 'modules/access_control/page.html') as page:
        await page.fill('#ac-consumer', 'reports')
        await page.click('#ac-load')
        await page.wait_for_function("() => document.getElementById('ac-status').textContent.startsWith('已处理')")
        group = await label(page, '#ac-names p:first-child details:nth-of-type(1)')
        member = await label(page, '#ac-names p:first-child details:nth-of-type(2)')
        listed = await page.text_content('#ac-names')
        assert await page.evaluate(NO_XSS) and page.errors == []
    assert group[1:] == ['group-a', 'group-a'] and member[1:] == ['member-a', 'member-a']
    assert group[0].startswith(expected[0]) and member[0].endswith(expected[1])
    assert listed.startswith('授权来源：群 ') and 'daily' not in listed
