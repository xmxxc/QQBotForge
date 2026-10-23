"""忍者必须死3 owner page: real DOM behaviour against the real plugin routes (fake data only)."""

import os
import shutil
from html.parser import HTMLParser
from pathlib import Path

import pytest
from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_ninja3 import ninja as ninja  # noqa: F401

PAGE = Path(__file__).absolute().parents[2] / 'plugins/ninja3/page.html'
# Named form controls override these HTMLFormElement members ([LegacyOverrideBuiltIns]).
FORM_METHODS = {'reset', 'submit', 'requestSubmit', 'checkValidity', 'reportValidity'}


def test_no_form_control_shadows_a_form_method():
    class Controls(HTMLParser):
        depth, shadowing = 0, []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'form':
                self.depth += 1
            elif self.depth and FORM_METHODS & {attrs.get('id'), attrs.get('name')}:
                self.shadowing.append((tag, attrs.get('id'), attrs.get('name')))

        def handle_endtag(self, tag):
            if tag == 'form':
                self.depth -= 1

    parser = Controls()
    parser.feed(PAGE.read_text(encoding='utf-8'))
    assert parser.shadowing == []


def chrome():
    path = os.environ.get('NINJA3_CHROME') or shutil.which('google-chrome') or shutil.which('chromium')
    if not path:
        pytest.skip('未找到 Chrome／Chromium，浏览器页面回归未运行')
    pytest.importorskip('playwright.async_api', reason='未安装 playwright，浏览器页面回归未运行')
    return path


async def test_account_form_new_edit_delete_and_rejection_in_a_real_browser(ninja):
    executable = chrome()
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from playwright.async_api import async_playwright

    from web import auth
    from web.api import handle_ext_route

    host = ninja
    await host.ninja_api('POST', 'groups', {'group': 'group-a', 'enabled': True})
    app = web.Application()

    async def index(request):
        return web.Response(text=PAGE.read_text(encoding='utf-8'), content_type='text/html')

    app.router.add_get('/', index)
    app.router.add_route('*', '/api/ext/{tail:.*}', handle_ext_route)
    server = TestServer(app, host='127.0.0.1')
    await server.start_server()
    base = str(server.make_url('/'))
    errors = []

    async def accounts():
        return {a['name']: a for a in (await host.ninja_api())[1]['data']}

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(executable_path=executable, headless=True)
            context = await browser.new_context()
            await context.add_cookies([{'name': auth.SESSION_COOKIE, 'value': 'group-test', 'url': base}])
            page = await context.new_page()
            page.set_default_timeout(5000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(base)
            await page.fill('#bot', 'bot-a')
            await page.click('#refresh')
            await page.wait_for_function("() => document.getElementById('status').textContent.startsWith('已加载')")

            async def save(expect):
                await page.click('#account button:not([type])')
                try:
                    await page.wait_for_function('text => document.getElementById("status").textContent.includes(text)', arg=expect)
                except Exception:
                    raise AssertionError(f'页面未显示“{expect}”：状态={await page.text_content("#status")!r} 页面错误={errors}') from None
                return await page.text_content('#status')

            async def fill(name, login, password):
                await page.fill('#name', name)
                await page.fill('#login', login)
                await page.fill('#password', password)

            # 1. Create A: the list refreshes and the status shows this save's actual sync result.
            await fill('账号A', 'fake-login-a', 'fake-pass-a')
            await page.check('#enabled')
            assert await save('账号已保存') == '账号已保存；授权同步：已同步本账号'
            assert '账号A' in await page.text_content('#rows')
            a = (await accounts())['账号A']
            # 2. Edit A, press 新增, fill B and save: a second ID; A's credentials untouched.
            await page.click('#rows tr:has-text("账号A") button:text("编辑")')
            await page.click('#account button:text("新增")')
            assert await page.input_value('#name') == ''
            await fill('账号B', 'fake-login-b', 'fake-pass-b')
            await save('账号已保存')
            saved = await accounts()
            assert set(saved) == {'账号A', '账号B'} and saved['账号B']['id'] != a['id']
            assert (saved['账号A']['id'], saved['账号A']['login']) == (a['id'], 'fake-login-a')
            stored = await host.ninja().store.get('bot-a', 'accounts', a['id'])
            assert stored['password'] == 'fake-pass-a'
            # 3. Editing changes only that record; the next 新增 leaves no old ID behind.
            await page.click('#rows tr:has-text("账号B") button:text("编辑")')
            await page.fill('#login', 'fake-login-b2')
            await save('账号已保存')
            saved = await accounts()
            assert (saved['账号A']['login'], saved['账号B']['login']) == ('fake-login-a', 'fake-login-b2') and len(saved) == 2
            await page.click('#account button:text("新增")')
            await fill('账号C', 'fake-login-c', 'fake-pass-c')
            await save('账号已保存')
            assert len(await accounts()) == 3
            # 4. Delete: list and form agree, no page error; 新增 still works afterwards.
            page.once('dialog', lambda dialog: dialog.accept())
            await page.click('#rows tr:has-text("账号C") button:text("删除")')
            await page.wait_for_function("() => !document.getElementById('rows').textContent.includes('账号C')")
            assert set(await accounts()) == {'账号A', '账号B'} and await page.input_value('#name') == ''
            await fill('账号D', 'fake-login-d', 'fake-pass-d')
            await save('账号已保存')
            assert set(await accounts()) == {'账号A', '账号B', '账号D'}
            # 5. A rejected save reports the error and leaves both server data and the form as they were.
            await page.click('#rows tr:has-text("账号D") button:text("编辑")')
            await page.fill('#name', '账号A')
            assert await save('名称不能重复') == '同一 Bot 下账号名称不能重复'
            assert await page.input_value('#name') == '账号A' and await page.input_value('#login') == 'fake-login-d'
            saved = await accounts()
            assert (saved['账号A']['id'], saved['账号D']['login']) == (a['id'], 'fake-login-d') and len(saved) == 3
            await browser.close()
    finally:
        await server.close()
    assert errors == []
