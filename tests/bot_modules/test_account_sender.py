"""Actual MessageSender payloads, with only its platform HTTP transport replaced."""

from types import SimpleNamespace

from account_host import account_host as account_host  # noqa: F401
from test_access_control import access_host as access_host  # noqa: F401
from test_account_audit_menus import grant
from test_account_c01 import bind


async def test_real_sender_directory_and_button_query_never_disclose_to_bystanders(account_host, monkeypatch):
    from core.message.sender import MessageSender

    h = account_host
    a = await h.create(name='private-title')
    await grant(h, a)
    await bind(h, 'group-a', 'member-a')
    sent = []

    async def transport(self, endpoint, data, **kwargs):
        sent.append((endpoint, data))
        return True, {'id': 'transport-message'}

    monkeypatch.setattr(MessageSender, 'post_json', transport)
    sender = MessageSender(SimpleNamespace(appid='bot-a'))
    h.application.get_bot('bot-a').sender = sender
    await h.event('账户列表', user='member-a', group='group-a')
    public = [str(p) for e, p in sent if '/groups/' in e]
    private = [(e, p) for e, p in sent if '/users/private-a/' in e]
    assert private and all(a['name'] not in p and a['id'] not in p for p in public)
    payload = private[-1][1]
    assert a['name'] in str(payload)
    buttons = payload['keyboard']['content']['rows']
    query = next(b['action']['data'] for row in buttons for b in row['buttons'] if b['render_data']['label'] == a['name'])
    sent.clear()
    await h.click(query, user='private-a')
    assert any('fake-secret' in str(p) for e, p in sent if '/users/private-a/' in e)
    assert all('fake-secret' not in str(p) for e, p in sent if '/groups/' in e)
    sent.clear()
    await h.click(query, user='stranger')
    assert all('fake-secret' not in str(p) for e, p in sent)
